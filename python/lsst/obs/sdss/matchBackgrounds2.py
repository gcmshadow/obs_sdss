# 
# LSST Data Management System
# Copyright 2008, 2009, 2010 LSST Corporation.
# 
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the LSST License Statement and 
# the GNU General Public License along with this program.  If not, 
# see <http://www.lsstcorp.org/LegalNotices/>.
import sys, os, re
import numpy as num

import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
import lsst.afw.geom as afwGeom
import lsst.afw.detection as afwDetect
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.ip.diffim as ipDiffim
from lsst.coadd.utils import Coadd, addToCoadd, setCoaddEdgeBits

from convertfpM import convertfpM
from convertasTrans import convertasTrans
from convertpsField import convertpsField
from scipy.interpolate import SmoothBivariateSpline
try:
    import pymssql 
except:
    print "You need pymssql installed to access the DB"
    sys.exit(1)

rootdir = "/astro/net/pogo1/stripe82/imaging"
db      = pymssql.connect(user="clue-1", password="wlZH2xWy", host="fatboy.npl.washington.edu", database="clue", port=1433)
cursor  = db.cursor()

class FieldMatch(object):
    def __init__(self, args):
        self.run    = args[0]
        self.rerun  = args[1]
        self.filt   = args[2]
        self.camcol = args[3]
        self.field  = args[4]
        self.strip  = args[5]

        self.fpC    = None
        self.fpM    = None
        self.wcs    = None
        self.psf    = None

    def loadfpC(self):
        self.fpC = getfpC(self.run, self.rerun, self.filt, self.camcol, self.field)

    def loadfpM(self):
        self.fpM = getfpM(self.run, self.rerun, self.filt, self.camcol, self.field)

    def loadWcs(self):
        asTrans = getasTrans(self.run, self.rerun)
        if asTrans:
            self.wcs = convertasTrans(asTrans, self.filt, self.camcol, self.field)

    def loadPsf(self):
        self.psf = getpsField(self.run, self.rerun, self.filt, self.camcol, self.field)
    
    def createExp(self, gain = 1.0):
        var  = afwImage.ImageF(self.fpC, True)
        var /= gain
        mi   = afwImage.MaskedImageF(self.fpC, self.fpM, var)
        exp  = afwImage.ExposureF(mi, self.wcs)
        exp.setPsf(self.psf)
        return exp

class MatchBackgroundsConfig(pexConfig.Config):
    warpingKernelName = pexConfig.Field(
        dtype = str,
        doc = """Type of kernel for remapping""",
        default = "lanczos3"
    )
    backgroundOrder = pexConfig.Field(
        dtype = int,
        doc = """Order of background Chebyshev""",
        default = 1
    )
    writeFits = pexConfig.Field(
        dtype = bool,
        doc = """Write output fits files""",
        default = True
    )
    outputPath = pexConfig.Field(
        dtype = str,
        doc = """Location of output files""",
        default = "/tmp"
    )

    refPsfSize = pexConfig.Field(
        dtype = int,
        doc = """Size of reference Psf matrix; must be same size as SDSS Psfs""",
        default = 31
    )
    refPsfSigma = pexConfig.Field(
        dtype = float,
        doc = """Gaussian sigma for Psf FWHM (pixels)""",
        default = 4.0
    )

    coadd = pexConfig.ConfigField(dtype = Coadd.ConfigClass, doc="")

class MatchBackgrounds(pipeBase.Task):
    ConfigClass = MatchBackgroundsConfig
    def __init__(self, refrun, rerun, camcol, filt, *args, **kwargs):
        pipeBase.Task.__init__(self, *args, **kwargs)
        self.refrun  = refrun
        self.rerun   = rerun
        self.camcol  = camcol
        self.filt    = filt
        self.asTrans = getasTrans(self.refrun, self.rerun)

        self.refExp          = {} # per field
        self.stitchedExp     = {} # per run
        self.warpedExp       = {} # per run
        self.bgMatchedExp    = {} # per run

        self.warper = afwMath.Warper(self.config.warpingKernelName)
        self.refPsf = afwDetect.createPsf("DoubleGaussian", self.config.refPsfSize, self.config.refPsfSize, self.config.refPsfSigma)
        
        config          = ipDiffim.ModelPsfMatchTask.ConfigClass()
        config.kernel.active.kernelSize = self.config.refPsfSize // 2
        self.psfMatcher = ipDiffim.ModelPsfMatchTask(config=config)
        
    @pipeBase.timeMethod
    def run(self, fields, nMax = 10, **kwargs):

        for field in fields:
            fpCRef = getfpC(self.refrun, self.rerun, self.filt, self.camcol, field)
            if not fpCRef:
                continue
            
            fpMRef = getfpM(self.refrun, self.rerun, self.filt, self.camcol, field)
            if not fpMRef:
                continue
            
            wcsRef = convertasTrans(self.asTrans, self.filt, self.camcol, field)
            if not wcsRef:
                continue

            psfRef = getpsField(self.refrun, self.rerun, self.filt, self.camcol, field)
            if not psfRef:
                continue
        
            # Assemble an exposure out of this
            # Unknown gain for now
            varRef = afwImage.ImageF(fpCRef, True)
            miRef  = afwImage.MaskedImageF(fpCRef, fpMRef, varRef)
            exp    = afwImage.ExposureF(miRef, wcsRef)
            exp.setPsf(psfRef)

            self.refExp[field]          = self.psfMatcher.run(exp, self.refPsf).psfMatchedExposure

            self.stitchedExp[field]     = {}
            self.warpedExp[field]       = {}
            self.bgMatchedExp[field]    = {}
            
            matches = self.queryClue(fpCRef.getBBox(), wcsRef, self.filt)
            self.processMatches(matches, field, nMax = nMax)

    @pipeBase.timeMethod
    def queryClue(self, bbox, wcs, filt):
        LLC = wcs.pixelToSky(bbox.getMinX(), bbox.getMinY())
        ULC = wcs.pixelToSky(bbox.getMinX(), bbox.getMaxY())
        URC = wcs.pixelToSky(bbox.getMaxX(), bbox.getMaxY())
        LRC = wcs.pixelToSky(bbox.getMaxX(), bbox.getMinY())
    
        sql  = "select run,rerun,filter,camCol,field,strip from clue.dbo.SeasonStripColor_bboxNoView WITH(INDEX(idx_bbox))"
        sql += " where bbox.STIntersects(geography::STGeomFromText('POLYGON (("
        sql += " %f %f," % (LLC[0].asDegrees(), LLC[1].asDegrees())
        sql += " %f %f," % (ULC[0].asDegrees(), ULC[1].asDegrees())
        sql += " %f %f," % (URC[0].asDegrees(), URC[1].asDegrees())
        sql += " %f %f," % (LRC[0].asDegrees(), LRC[1].asDegrees())
        sql += " %f %f ))', 4326))=1" % (LLC[0].asDegrees(), LLC[1].asDegrees())
        sql += " and filter='%s'" % (filt)
        sql += " order by run asc, field asc;"   
        
        print sql
        cursor.execute(sql)
        results = cursor.fetchall()
    
        # Note: lets just add each strip up and we'll use the N/S overlap
        # to match coadd backgroudns
    
        amatches = []
        for result in results:
            amatches.append(FieldMatch(result))
    
        strips = num.array([x.strip for x in amatches])
        runs   = num.array([x.run for x in amatches])
        idx    = num.where(runs == self.refrun)[0]
        strip  = list(set(strips[idx]))
        if len(strip) != 1:
            print "ERROR in strips"
            sys.exit(1)
    
        idxs     = num.where(strips == strip)[0]
        smatches = []
        for idx in idxs:
            smatches.append(amatches[idx])
        return smatches

    @pipeBase.timeMethod
    def processMatches(self, matches, field, nMax = None, gain = 1.0, overlap = 128, testme = 0):
        runs  = num.array([x.run for x in matches])
        uruns = list(set(runs))
        uruns.sort()

        if self.config.writeFits:
            self.refExp[field].writeFits(os.path.join(self.config.outputPath, 
                                                      "exp-%06d-%s%d-%04d.fits" % 
                                                      (self.refrun, self.filt, self.camcol, field)))
        nProc = 0
        for run in uruns:
            if nMax and nProc >= nMax:
                break

            print "RUNNING", run, "vs.", self.refrun, nProc

            if run == self.refrun:
                continue

            #if run != 1755:
            #    continue
            
            idxs = num.where(runs == run)[0]
            if len(idxs) == 0:
                continue

            runMatches = []
            for idx in idxs:
                runMatches.append(matches[idx])

            nloaded = 0
            for match in runMatches:
                match.loadfpC()
                match.loadfpM()
                match.loadWcs()
                match.loadPsf()
                if match.fpC and match.fpM and match.wcs and match.psf:
                    nloaded += 1
    
            if nloaded != len(runMatches):
                print "Not able to load all images, skipping to next run"
                continue

            # Stitching together neighboring images from the matching run
            width  = runMatches[0].fpC.getWidth()
            height = runMatches[0].fpC.getHeight() * nloaded - overlap * (nloaded - 1) + testme * nloaded
            stitch = afwImage.MaskedImageF(width, height)
    
            for i in range(len(runMatches)):
                match  = runMatches[i]
                matchExp = match.createExp(gain)
                # Psf match before stitching!
                psfmatchedExp = self.psfMatcher.run(matchExp, self.refPsf).psfMatchedExposure
                
                symin  = (i + 0) * match.fpC.getHeight() + (i * testme)
                symax  = (i + 1) * match.fpC.getHeight() + (i * testme)
    
                iymin  = 0
                iymax  = match.fpC.getHeight()
                
                if i > 0:
                    iymin   = overlap
                    symin  -= (i - 1) * overlap
                    symax  -= (i - 0) * overlap
    
                # Note transpose of getArray()
                try:
                    stitch.getImage().getArray()[symin:symax,:]    = psfmatchedExp.getMaskedImage().getImage().getArray()[iymin:iymax,:]
                    stitch.getMask().getArray()[symin:symax,:]     = psfmatchedExp.getMaskedImage().getMask().getArray()[iymin:iymax,:]
                    stitch.getVariance().getArray()[symin:symax,:] = psfmatchedExp.getMaskedImage().getVariance().getArray()[iymin:iymax,:]
                except:
                    import pdb; pdb.set_trace()
                    
                # Clear up memory
                match.fpC = None
                match.fpM = None

            # Keep Wcs of first image
            exp = afwImage.ExposureF(stitch, runMatches[0].wcs)
            exp.setPsf(self.refPsf)

            # Memory hog!
            # self.stitchedExp[field][run] = exp

            if self.config.writeFits:
                exp.writeFits(os.path.join(self.config.outputPath, 
                                           "psfmatch-%06d-%s%d-%04d-r%06d.fits" % 
                                           (self.refrun, self.filt, self.camcol, field, run)))

            # Do need to keep this
            self.warpedExp[field][run]   = self.warper.warpExposure(self.refExp[field].getWcs(), 
                                                                    exp, 
                                                                    destBBox = self.refExp[field].getBBox(afwImage.PARENT))

            # Do after warping, since it loses it in warping
            self.warpedExp[field][run].setPsf(self.refPsf)

            if self.config.writeFits:
                self.warpedExp[field][run].writeFits(os.path.join(self.config.outputPath, 
                                                                  "warp-%06d-%s%d-%04d-r%06d.fits" % 
                                                                  (self.refrun, self.filt, self.camcol, field, run)))
            nProc += 1


    @pipeBase.timeMethod   
    def matchBackgrounds(self, field, binsize = 256):
        refMask  = self.refExp[field].getMaskedImage().getMask().getArray()
        refArr   = self.refExp[field].getMaskedImage().getImage().getArray()

        # Basic; only use pixels that are unmasked in *all* images
        # Less basic; look for certain bits flipped.  TBD...
        runsToMatch = self.warpedExp[field].keys()
        expsToMatch = self.warpedExp[field].values()

        skyMask  = num.sum(num.array([x.getMaskedImage().getMask().getArray() for x in expsToMatch]), 0)
        skyArr   = num.array([x.getMaskedImage().getImage().getArray() for x in expsToMatch])
        Nim      = len(skyArr)

        # Find all unmasked (sky) pixels
        idx = num.where((refMask + skyMask) == 0)

        width  = self.refExp[field].getMaskedImage().getWidth()
        height = self.refExp[field].getMaskedImage().getHeight()
        nbinx  = width  // binsize
        nbiny  = height // binsize

        bgX  = num.zeros((nbinx*nbiny, Nim)) # coord
        bgY  = num.zeros((nbinx*nbiny, Nim)) # coord
        bgZ  = num.zeros((nbinx*nbiny, Nim)) # value
        bgdZ = num.zeros((nbinx*nbiny, Nim)) # unc

        for biny in range(nbiny):
            ymin = biny * binsize
            ymax = min((biny + 1) * binsize, self.refExp[field].getMaskedImage().getHeight())
            idxy = num.where( (idx[0] >= ymin) & (idx[0] < ymax) )[0]

            for binx in range(nbinx):
                xmin   = binx * binsize
                xmax   = min((binx + 1) * binsize, self.refExp[field].getMaskedImage().getWidth())
                idxx   = num.where( (idx[1] >= xmin) & (idx[1] < xmax) )[0]
                inreg  = num.intersect1d(idxx, idxy)

                Aij    = num.zeros((Nim, Nim))
                Eij    = num.ones((Nim, Nim))

                area0 = refArr[idx[0][inreg],idx[1][inreg]]
                for i in range(Nim):
                    areai     = skyArr[i][idx[0][inreg],idx[1][inreg]]
                    area      = area0 - areai
                    
                    bgX [binx + biny * nbinx, i] = 0.5 * (xmin + xmax)
                    bgY [binx + biny * nbinx, i] = 0.5 * (ymin + ymax) 
                    bgZ [binx + biny * nbinx, i] = num.mean(area) 
                    bgdZ[binx + biny * nbinx, i] = num.std(area) 

        # Function for each image
        for i in range(Nim):

            # Function for this image
            bbox  = afwGeom.Box2D(self.refExp[field].getBBox())
            poly  = afwMath.Chebyshev1Function2D(self.config.backgroundOrder, bbox)          
            terms = list(poly.getParameters())

            Nall  = nbiny * nbinx
            Ncell = num.sum(num.isfinite(bgZ[:,i]))
            Nterm = len(terms)

            # Mx = b; solve for x
            m  = num.zeros((Ncell, Nterm))
            b  = num.zeros((Ncell))
            iv = num.zeros((Ncell))

            # One constraint for each cell
            nc = 0
            for na in range(Nall):
                if not num.isfinite(bgZ[:,i][na]):
                    continue

                for nt in range(Nterm):
                    terms[nt] = 1.0
                    poly.setParameters(terms)
                    m[nc, nt] = poly(bgX[:,i][na], bgY[:,i][na])
                    terms[nt] = 0.0
                b[nc]  = bgZ[:,i][na]
                iv[nc] = 1.0 / (bgdZ[:,i][na])**2
                nc += 1
            #import pdb; pdb.set_trace()

            M    = num.dot(num.dot(m.T, num.diag(iv)), m)
            B    = num.dot(num.dot(m.T, num.diag(iv)), b)
            Minv = num.linalg.inv(M)
            Soln = num.dot(Minv, B)
            poly.setParameters(Soln)

            run = runsToMatch[i]
            exp = expsToMatch[i]
            im  = exp.getMaskedImage()
            im += poly

            # Clear memory
            self.warpedExp[field][run] = None
            # But replace it with warped
            self.bgMatchedExp[field][run] = exp

            if self.config.writeFits:
                exp.writeFits(os.path.join(self.config.outputPath, 
                                           "match-%06d-%s%d-%04d-r%06d.fits" % 
                                           (self.refrun, self.filt, self.camcol, field, run)))
            # DEBUGGING INFO
            tmp  = afwImage.MaskedImageF(im, True)
            tmp -= self.refExp[field].getMaskedImage()

            if self.config.writeFits:
                tmp.writeFits(os.path.join(self.config.outputPath, 
                                           "diff-%06d-%s%d-%04d-r%06d.fits" % 
                                           (self.refrun, self.filt, self.camcol, field, run)))
            
            # Lets see some stats!
            area = tmp.getImage().getArray()[idx]
            print run, num.mean(area), num.median(area), num.std(area), len(area)

    @pipeBase.timeMethod   
    def createCoadd(self, field):
        runsToMatch = self.bgMatchedExp[field].keys()
        expsToMatch = self.bgMatchedExp[field].values()

        refMi    = self.refExp[field].getMaskedImage()
        coaddMi  = refMi.Factory(refMi.getBBox(afwImage.PARENT))
        weightIm = coaddMi.getImage().Factory(coaddMi.getBBox(afwImage.PARENT))
        weight   = 1.0

        badPixelMask = afwImage.MaskU.getPlaneBitMask(self.config.coadd.badMaskPlanes)

        addToCoadd(coaddMi, weightIm, refMi, badPixelMask, weight)

        for i in range(len(runsToMatch)):
            addToCoadd(coaddMi, weightIm, expsToMatch[i].getMaskedImage(), badPixelMask, weight)

        # Post processing
        coaddMi /= weightIm
        setCoaddEdgeBits(coaddMi.getMask(), weightIm)

        
        if self.config.writeFits:
            coaddMi.writeFits(os.path.join(self.config.outputPath, 
                                           "coadd-%06d-%s%d-%04d.fits" % 
                                           (self.refrun, self.filt, self.camcol, field)))

            
        

                    #Aij[0][i] = num.mean(area)
                    #Eij[0][i] = num.std(area)
                    #Aij[i][0] = -1 * Aij[0][i]
                    #Eij[i][0] = +1 * Eij[0][i]

# NN2 BELOW, NOT YET WORKING            
        
#                    print binx, biny, i, num.mean(area0), num.mean(areai), num.mean(area), num.mean(area0)-num.mean(areai)
#                    bgs1X.append(num.mean(area0) - num.mean(areai))
#
#                    for j in range(i+1, N):
#                        areaj     = skyArr[j-1][idx[0][inreg],idx[1][inreg]]
#                        area      = areai - areaj
#                        Aij[i][j] = num.mean(area)
#                        Eij[i][j] = num.std(area)
#                        Aij[j][i] = -1 * Aij[i][j]
#                        Eij[j][i] = +1 * Eij[i][j]
#                        
#                Eij2     = Eij**2
#                InvEhat2 = 2.0 / (N * (N - 1.0)) / num.sum(num.triu(Eij2)) # NOTE TRIU DOES INCLUDE DIAGONALS!!!
#                InvEhat2 = 1.0
#
#                Cij   = -1. / Eij2
#                for i in range(N): Cij[i][i] = 0.0
#                Cij  += num.diag(1.0 / num.sum(Eij2, 0))
#                Cij  += InvEhat2
#                Cinv  = num.linalg.inv(Cij)
#                AEij  = Aij / Eij2
#                for i in range(N): AEij[i][i] = 0.0
#                bgs   = num.sum(num.dot(Cinv, AEij), 0)
#                unc   = num.sqrt(num.diagonal(Cinv))
#                
#                import pdb; pdb.set_trace()
#
#                print num.sum(bgs)
#                bgs  -= bgs[0]
#                print bgs
#                print
#                # Note that these numbers are differences between
#                # image reference in a lightcurve sense.  So negative
#                # numbers means the background is fainter.  So you
#                # need to subtract the numbers from the images to get
#                # them to match.
#                bgs *= -1
#
#                bgs1.append(bgs1X)
#                bgs2.append(bgs)
#
#        bgs1     = num.array(bgs1)
#        bgs2     = num.array(bgs2)
#        offsets1 = num.mean(bgs1, 0)
#        offsets2 = num.mean(bgs2, 0)
#
#        # debugging!
#        self.refExp[field].writeFits("/tmp/refexp.fits")
#        for i in range(1, len(offsets1)):
#            comp1  = afwImage.MaskedImageF(self.warpedExp[field].values()[i-1].getMaskedImage(), True)
#            comp1 += offsets1[i]
#            comp1.writeFits("/tmp/comp_avg_%d.fits" % (i))
#
#            comp2  = afwImage.MaskedImageF(self.warpedExp[field].values()[i-1].getMaskedImage(), True)
#            comp2 += offsets2[i]
#            comp2.writeFits("/tmp/comp_nn2_%d.fits" % (i))
#
#        import pdb; pdb.set_trace()
        

######
######
######
######

def getfpC(run, rerun, filt, camcol, field):
    fname = os.path.join(rootdir, str(run), str(rerun), "corr", str(camcol), "fpC-%06d-%s%d-%04d.fit.gz" % (run, filt, camcol, field))
    print fname
    if os.path.isfile(fname):
        im  = afwImage.ImageF(fname)
        im -= 1000 # damn pedestal
        return im
    return None

def getfpM(run, rerun, filt, camcol, field):
    fname1 = os.path.join(rootdir, str(run), str(rerun), "objcs", str(camcol), "fpM-%06d-%s%d-%04d.fit" % (run, filt, camcol, field))
    fname2 = os.path.join(rootdir, str(run), str(rerun), "objcs", str(camcol), "fpM-%06d-%s%d-%04d.fit.gz" % (run, filt, camcol, field))
    for fname in (fname1, fname2):
        print fname
        if os.path.isfile(fname):
            try:
                return convertfpM(fname, allPlanes = True)
            except:
                return None
    return None

def getasTrans(run, rerun):
    fname = os.path.join(rootdir, str(run), str(rerun), "astrom", "asTrans-%06d.fit" % (run))
    print fname
    if os.path.isfile(fname):
        return fname
    return None

def getpsField(run, rerun, filt, camcol, field):
    fname = os.path.join(rootdir, str(run), str(rerun), "objcs", str(camcol), "psField-%06d-%d-%04d.fit" % (run, camcol, field))
    print fname
    if os.path.isfile(fname):
        return convertpsField(fname, filt)
    return None


if __name__ == '__main__':
    refrun  = int(sys.argv[1])
    camcol  = int(sys.argv[2])
    filt    = sys.argv[3]

    matcher = MatchBackgrounds(refrun, 40, camcol, filt)
    fields  = range(1, 1000)
    fields  = [11,]
    if True:
        matcher.run(fields, nMax = 1000)
        matcher.matchBackgrounds(fields[0])
        matcher.createCoadd(fields[0])
        sys.exit(1)
    else:
        # If you have stuff in the output dir, use it...
        matcher.warpedExp[fields[0]] = {}
        matcher.bgMatchedExp[fields[0]] = {}
        tmpdir  = "/tmp/"
        nmax    = 100
        nfound  = 0
        for f in os.listdir(tmpdir):
            if f.startswith("exp-%06d" % (refrun)):
                refExp = afwImage.ExposureF(os.path.join(tmpdir, f))
                matcher.refExp[fields[0]] = refExp
            elif f.startswith("warp-%06d" % (refrun)) and nfound < nmax:
                skyExp = afwImage.ExposureF(os.path.join(tmpdir, f))
                skyrun  = int(re.sub("r", "", f.split("-")[4].split(".")[0]))
                matcher.warpedExp[fields[0]][skyrun] = skyExp
                nfound += 1
        matcher.matchBackgrounds(fields[0])
    
