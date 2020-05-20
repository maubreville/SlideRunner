import openslide
import multiprocessing
import queue
import time
import numpy as np
import os
from . import dicom
from . import cellvizio

class RotatableOpenSlide(object):

    def __new__(cls, filename, rotate):
        if cls is RotatableOpenSlide:
            bname, ext = os.path.splitext(filename)
            if ext.upper() == '.DCM': return type("RotatableOpenSlide", (RotatableOpenSlide, dicom.ReadableDicomDataset,openslide.ImageSlide), {})(filename, rotate)
            if ext.upper() == '.MKT': return type("ReadableCellVizioMKTDataset", (RotatableOpenSlide, cellvizio.ReadableCellVizioMKTDataset, openslide.ImageSlide), {})(filename,rotate)
            try:
                slideobj = type("OpenSlide", (RotatableOpenSlide,openslide.OpenSlide), {})(filename, rotate)
                return slideobj
            except:
                return type("ImageSlide", (RotatableOpenSlide,openslide.ImageSlide), {})(filename, rotate)
        else:
            return object.__new__(cls)

    def __init__(self, filename, rotate=False):
        self.rotate=rotate
        self.type=0
        self.numberOfFrames = 1
        self.fps = 1.0
        return super().__init__(filename)




    # Implements 180 degree rotated version of read_region
    def read_region(self, location, level, size, zLevel=0):
        # zlevel is ignored for SVS files
        if (self.rotate):
            location = [int(x-y-(w*self.level_downsamples[level])) for x,y,w in zip(self.dimensions, location, size)]
            return super().read_region(location, level, size, zLevel).rotate(180)
        else:
            return super().read_region(location, level, size, zLevel)

    def transformCoordinates(self, location, level=0, size=None, inverse=False):
        if (self.rotate):
            retarr = np.copy(location)
            retarr[:,0] = self.dimensions[0]-retarr[:,0]
            retarr[:,1] = self.dimensions[1]-retarr[:,1]
            return retarr
        else:
            return location

    def slide_center(self):
        return [int(x/2) for x in self.dimensions]
    
    def read_centerregion(self, location, level, size, center=None, zLevel=0):
        center = self.slide_center() if center is None else center
    #    print('Offset to center location:', [self.level_downsamples[level]*s for s in size], self.level_downsamples[level])
        return self.read_region([int(x-s*self.level_downsamples[level]/2-d) for x,d,s in zip(center,location, size)], level, size, zLevel)
    

class SlideReader(multiprocessing.Process):
    def __init__(self):
        multiprocessing.Process.__init__(self)
        self.sl = None
        self.slidename = None
        self.slide = None
        self.daemon=True
        self.queue = multiprocessing.Queue(50)
        self.outputQueue = multiprocessing.Queue()

    def run(self):
        img=None
        lastReq = [(-1,-1),-1,(512,512)]
        while (True):
            (slidename, location, level, size, id, rotated, zlevel) = self.queue.get()

            try:
                while(True):
                    (slidename, location, level, size, id, rotated, zlevel) = self.queue.get(True,0.01)
            except queue.Empty:
                pass


            if (slidename==-1):
                print('Exiting SlideReader thread')
                return

            if (slidename!=self.slidename):
                self.slide = RotatableOpenSlide(slidename, rotate=rotated)
                self.slidename = slidename
            self.slide.rotate = rotated

            if not all([a==b for a,b in zip([location,level,size,zlevel],lastReq)]):
                img = self.slide.read_region(location, level, size, zLevel=zlevel)
                lastReq = [location, level, size,zlevel]

            self.outputQueue.put((np.array(img),id))

