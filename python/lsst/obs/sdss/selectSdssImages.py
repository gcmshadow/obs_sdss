#!/usr/bin/env python
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#
import MySQLdb

import lsst.pex.config as pexConfig
from lsst.afw.coord import IcrsCoord
import lsst.afw.geom as afwGeom
from lsst.daf.persistence import DbAuth
import lsst.pipe.base as pipeBase
from lsst.pipe.tasks.selectImages import BaseSelectImagesTask, BaseExposureInfo

__all__ = ["SelectSdssImagesTask"]

class SelectSdssImagesConfig(BaseSelectImagesTask.ConfigClass):
    """Config for SelectSdssImagesTask
    """
    table = pexConfig.Field(
        doc = "Name of database table",
        dtype = str,
        default = "SeasonFieldQuality_Test",
    )
    maxFwhm = pexConfig.Field(
        doc = "maximum FWHM (arcsec)",
        dtype = float,
        optional = True,
    )
    maxSky = pexConfig.Field(
        doc = "maximum sky level (maggies/arcsec^2)",
        dtype = float,
        optional = True,
    )
    maxAirmass = pexConfig.Field(
        doc = "Maximum airmass",
        dtype = float,
        optional = True,
    )
    quality = pexConfig.ChoiceField(
        doc = "SDSS quality flag",
        dtype = int,
        default = 3,
        allowed={1:"All data", 2:"Flagged ACCEPTABLE or GOOD", 3:"Flagged GOOD"},
    )
    cullBlacklisted = pexConfig.Field(
        doc = "Omit blacklisted images? (Some run/field combinations have been blacklisted even though the quality metrics may have missed them.)",
        dtype = bool,
        default = True,
    )
    camcols = pexConfig.ListField(
        doc = "Which camcols to include? If None then include all",
        dtype = int,
        optional = True,
    )
    strip = pexConfig.Field(
        doc = "Strip: N, S or None for both",
        dtype = str,
        optional = True,
    )

    def setDefaults(self):
        BaseSelectImagesTask.ConfigClass.setDefaults(self)
        self.host = "lsst-db.ncsa.illinois.edu"
        self.port = 3306
        self.database = "krughoff_SDSS_quality_db"


class ExposureInfo(BaseExposureInfo):
    """Data about a selected exposure
    
    Data includes:
    - dataId: data ID of exposure (a dict)
    - coordList: a list of corner coordinates of the exposure (list of IcrsCoord)
    - fwhm: mean FWHM of exposure
    - quality: quality field from SeasonFieldQuality_Test table
    """
    def __init__(self, result):
        """Set exposure information based on a query result from a db connection
        """
        BaseExposureInfo.__init__(self)
        self.dataId = dict(
           run = result[self._nextInd],
           rerun = result[self._nextInd],
           camcol = result[self._nextInd],
           field = result[self._nextInd],
           filter = result[self._nextInd],
        )
        self.coordList = []
        for i in range(4):
            self.coordList.append(
                IcrsCoord(
                    afwGeom.Angle(result[self._nextInd], afwGeom.degrees),
                    afwGeom.Angle(result[self._nextInd], afwGeom.degrees),
                )
            )
        self.fwhm = result[self._nextInd]
        self.sky = result[self._nextInd]
        self.airmass = result[self._nextInd]
        self.quality = result[self._nextInd]
        self.isblacklisted = result[self._nextInd]

    @staticmethod
    def getColumnNames():
        """Get database columns to retrieve, in a format useful to the database interface
        
        @return database column names as list of strings
        """
        return ", ".join(
            "run rerun camcol field filter ra1 dec1 ra2 dec2 ra3 dec3 ra4 dec4".split() + \
            "psfWidth sky airmass quality isblacklisted".split()
        )


class SelectSdssImagesTask(BaseSelectImagesTask):
    """Select SDSS images suitable for coaddition
    """
    ConfigClass = SelectSdssImagesConfig
    
    @pipeBase.timeMethod
    def run(self, coordList, filter):
        """Select SDSS images suitable for coaddition in a particular region
        
        @param[in] filter: filter for images (one of "u", "g", "r", "i" or "z")
        @param[in] coordList: list of coordinates defining region of interest
        
        @return a pipeBase Struct containing:
        - exposureInfoList: a list of ExposureInfo objects
        """
        db = MySQLdb.connect(
            host = self.config.host,
            port = self.config.port,
            user = DbAuth.username(self.config.host, str(self.config.port)),
            passwd = DbAuth.password(self.config.host, str(self.config.port)),
            db = self.config.database,
        )
        cursor = db.cursor()

        if coordList is not None:
            # look for exposures that overlap the specified region

            # create table scisql.Region containing patch region
            coordStrList = ["%s, %s" % (c.getLongitude().asDegrees(),
                                        c.getLatitude().asDegrees()) for c in coordList]
            coordStr = ", ".join(coordStrList)
            coordCmd = "call scisql.scisql_s2CPolyRegion(scisql_s2CPolyToBin(%s), 10)" % (coordStr,)
            cursor.execute(coordCmd)
            cursor.nextset() # ignore one-line result of coordCmd
        
            # find exposures
            queryStr = ("""select %s
from SeasonFieldQuality_Test as ccdExp,
    (select distinct fieldid
    from SeasonFieldQuality_To_Htm10 as ccdHtm inner join scisql.Region
    on (ccdHtm.htmId10 between scisql.Region.htmMin and scisql.Region.htmMax)
    where ccdHtm.filter = \"%s\") as idList
where ccdExp.fieldid = idList.fieldid and """ % (ExposureInfo.getColumnNames(), filter))
        else:
            # no region specified; look over the whole sky
            queryStr = ("""select %s
from SeasonFieldQuality_Test where """ % ExposureInfo.getColumnNames())
        
        # compute where clauses as a list of (clause, data)
        whereDataList = [
            ("filter = %s", filter),
        ]

        if self.config.maxFwhm is not None:
            whereDataList.append(("psfWidth < %s", self.config.maxFwhm))

        if self.config.maxSky is not None:
            whereDataList.append(("sky < %s", self.config.maxSky))

        if self.config.maxAirmass is not None:
            whereDataList.append(("airmass < %s", self.config.maxAirmass))

        qualityTuple = tuple(range(self.config.quality, 4))
        whereDataList.append(_whereDataFromList("quality", qualityTuple))

        if self.config.cullBlacklisted:
            whereDataList.append(("isblacklisted = %s", False))

        if self.config.camcols is not None:
            whereDataList.append(_whereDataFromList("camcol", self.config.camcols))
        
        if self.config.strip is not None:
            whereDataList.append(("strip = %s", self.config.strip))
        
        queryStr += " and ".join(wd[0] for wd in whereDataList)
        dataTuple = tuple(wd[1] for wd in whereDataList)
        
        if self.config.maxExposures:
            queryStr += " limit %s" % (self.config.maxExposures,)
        
        self.log.log(self.log.INFO, "queryStr=%r; dataTuple=%s" % (queryStr, dataTuple))

        cursor.execute(queryStr, dataTuple)
        exposureInfoList = [ExposureInfo(result) for result in cursor]

        return pipeBase.Struct(
            exposureInfoList = exposureInfoList,
        )

    def _runArgDictFromDataId(self, dataId):
        """Extract keyword arguments for run (other than coordList) from a data ID
        
        @return keyword arguments for run (other than coordList), as a dict
        """
        return dict(
            filter = dataId["filter"]
        )

def _formatList(valueList):
    """Format a value list as "v0,v1,v2...vlast"
    """
    return ",".join(str(v) for v in valueList)

def _whereDataFromList(name, valueList):
    """Return a where clause and associated value(s)
    
    For example if valueList has a single value then returns
        "name = %s", valueList[0]
    but if valueList has more values then returns
        "name in %s", valueList
    
    This function exists because MySQL requires multiple values for "in" clauses.
    
    @raise RuntimeError if valueList is None or has length 0
    """
    if not valueList:
        raise RuntimeError("%s valueList = %s; must be a list with at least one value" % (name, valueList))

    if len(valueList) == 1:
        return ("%s = %%s" % (name,), valueList[0])
    else:
        return ("%s in %%s" % (name,), tuple(valueList))
    