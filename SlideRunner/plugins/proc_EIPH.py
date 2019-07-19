import SlideRunner.general.SlideRunnerPlugin as SlideRunnerPlugin
import queue
from threading import Thread
from queue import Queue
import numpy as np
import openslide
import cv2
import pickle

import torch
import torchvision.transforms as transforms

import SlideRunner.dataAccess.annotations as annotations

from helper.object_detection_helper import *
from loss.RetinaNetFocalLoss import RetinaNetFocalLoss
from models.RetinaNet import RetinaNet
from helper.nms_center_distance import non_max_suppression_by_distance



class Plugin(SlideRunnerPlugin.SlideRunnerPlugin):
    version = 0.1
    shortName = 'EIPH'
    inQueue = Queue()
    outQueue = Queue()
    updateTimer = 0.5
    outputType = SlideRunnerPlugin.PluginOutputType.RGB_IMAGE
    description = 'EIPH Analysis'
    pluginType = SlideRunnerPlugin.PluginTypes.IMAGE_PLUGIN
    configurationList = list((SlideRunnerPlugin.ComboboxPluginConfigurationEntry(uid='Datasource', name='Datasource',
                                                        options=["DATABASE", "INFERENCE"]),
                              SlideRunnerPlugin.TablePluginConfigurationEntry(uid=0, name='Test 123'),
                              SlideRunnerPlugin.PluginConfigurationEntry(uid='Headmap_Resolution', name='Headmap Resolution',
                                                                         initValue=1024, minValue=128,
                                                                         maxValue=8192),
                              SlideRunnerPlugin.FilePickerConfigurationEntry(uid='file', name='Model file',
                                                                             mask='*.pth'),
                              SlideRunnerPlugin.FilePickerConfigurationEntry(uid='stats', name='Stats file',
                                                                             mask='*.p'),
                              SlideRunnerPlugin.PushbuttonPluginConfigurationEntry(uid="Predict",
                                                                                   name="Predict field of View"),
                              #SlideRunnerPlugin.PushbuttonPluginConfigurationEntry(uid="PredictWSI",
                              #                                                     name="Predict WSI"),
                              SlideRunnerPlugin.PluginConfigurationEntry(uid='detect_thresh', name='Detection threshold',
                                                                         initValue=0.5, minValue=0.0, maxValue=1.0),
                              SlideRunnerPlugin.PluginConfigurationEntry(uid='nms_thresh', name='NMS by distance',
                                                                         initValue=75, minValue=15, maxValue=250),
                              #SlideRunnerPlugin.PushbuttonPluginConfigurationEntry(uid="NMS",
                              #                                                     name="Non maxima supression")
                              ))



    def __init__(self, statusQueue: Queue):
        self.statusQueue = statusQueue
        self.p = Thread(target=self.queueWorker, daemon=True)
        self.p.start()

        self.last_slideFilename = None
        self.level_dimension = None
        self.down_factor = None
        self.slide = None
        self.model = None
        self.anchors = None
        self.mean = None
        self.std = None
        self.shape = None
        self.level = 1
        self.annos = list()
        self.annos_original =  list()
        self.overlap = 0.75

        self.data_source = "DATABASE"

        self.headmap_resolution = 1024
        self.overlay = None
        self.classes = {3: 0, 4: 1, 5: 2, 6: 3, 7: 4}

        #[255,255,0,255],[255,0,255,255],[0,127,0,255],[255,127,0,255],[127,127,0,255],[255,200,200,255],[10, 166, 168,255],[
        self.annotationLabels = {
            0: SlideRunnerPlugin.PluginAnnotationLabel(0, '0', [255, 255, 0, 255]),
            1: SlideRunnerPlugin.PluginAnnotationLabel(1, '1', [255, 0, 255, 255]),
            2: SlideRunnerPlugin.PluginAnnotationLabel(2, '2', [0, 127, 0, 255]),
            3: SlideRunnerPlugin.PluginAnnotationLabel(3, '3', [255, 127, 0, 255]),
            4: SlideRunnerPlugin.PluginAnnotationLabel(4, '4', [127, 127, 0, 255]),
        }

        self.sendAnnotationLabelUpdate()


    def getAnnotationUpdatePolicy():
          # This is important to tell SlideRunner that he needs to update for every change in position.
          return SlideRunnerPlugin.AnnotationUpdatePolicy.UPDATE_ON_SLIDE_CHANGE

    def queueWorker(self):
        quitSignal = False
        last_annotations_count = 0
        last_annos_original_count = 0
        last_ModelName = ''
        last_StatsName = ''
        uid = 100000

        while not quitSignal:
            job = SlideRunnerPlugin.pluginJob(self.inQueue.get())
            #image = job.currentImage[:, :, :3]

            if (job.jobDescription == SlideRunnerPlugin.JobDescription.QUIT_PLUGIN_THREAD):
                # signal to exit this thread
                quitSignal = True
                continue

            nms_thresh = job.configuration["nms_thresh"]
            detect_thresh = job.configuration["nms_thresh"]

            if job.slideFilename != self.last_slideFilename:
                self.last_slideFilename = job.slideFilename
                self.slide = openslide.open_slide(job.slideFilename)
                self.level_dimension = self.slide.level_dimensions[0]

                self.down_factor = self.slide.level_downsamples[self.level]

                self.annos = list()
                self.annos_original = list()

            if job.trigger is not None and \
                    job.trigger.uid == "NMS":

                self.performe_nms(nms_thresh)
                self.updateAnnotations()

            if job.trigger is not None and \
                    job.trigger.uid == "Datasource":
                self.data_source = job.trigger.selected_value



            #refresh overview image
            if (len(job.openedDatabase.annotations) != last_annotations_count) \
                    or int(job.configuration['Headmap_Resolution']) != self.headmap_resolution\
                    or len(self.annos_original) !=  last_annos_original_count:
                self.overlay = self.create_overlay(job, self.data_source)


            if job.configuration['file'] != last_ModelName:
                last_ModelName = job.configuration['file']

                model_weights = torch.load(last_ModelName, map_location='cpu') \
                    if torch.cuda.is_available() \
                    else torch.load(last_ModelName)

                model_weights = model_weights['model']

                encoder = create_body(models.resnet18, True, -2)
                self.model = RetinaNet(encoder, 6, n_anchors=3, sizes=[32], chs=32, final_bias=-4., n_conv=2)
                self.model.load_state_dict(model_weights)

            if job.configuration['stats'] != last_StatsName:
                last_StatsName = job.configuration['stats']

                statistics = pickle.load(open(last_StatsName, "rb"))

                self.anchors = statistics["anchors"]
                self.mean = statistics["mean"]
                self.std = statistics["std"]
                self.shape = statistics["size"]

            if self.model is not None            \
                    and self.anchors is not None \
                    and self.mean is not None \
                    and self.std is not None \
                    and self.shape is not None and \
                    job.trigger is not None and \
                    job.trigger.uid == "Predict":

                self.perform_inference(job, nms_thresh, uid)


            result_dict = self.calculate_eiph_statistics(job, self.data_source)
            self.updateInformation(result_dict)
            self.updateAnnotations()

            self.setProgressBar(-1)

    def perform_inference(self, job, nms_thresh, uid):

        x_steps = range(int(job.coordinates[0]), int(job.coordinates[0] + job.coordinates[2]),
                        int(self.shape * self.down_factor * self.overlap))
        y_steps = range(int(job.coordinates[1]), int(job.coordinates[1] + job.coordinates[3]),
                        int(self.shape * self.down_factor * self.overlap))
        patches = []
        x_coordinates = []
        y_coordinates = []
        self.setMessage('Divide WSI into patches')
        for x in x_steps:
            for y in y_steps:
                patch = np.array(self.slide.read_region(location=(int(x), int(y)),
                                                        level=self.level, size=(self.shape, self.shape)))[:, :, :3]

                patch = pil2tensor(patch / 255., np.float32)
                patch = transforms.Normalize(self.mean, self.std)(patch)

                patches.append(patch[None, :, :, :])
                x_coordinates.append(x)
                y_coordinates.append(y)

                self.setProgressBar((len(patches) / (len(x_steps) * len(y_steps))) / 2 * 100)
        self.setMessage('Performe inference on {} patches'.format(len(patches)))
        class_pred_batch, bbox_pred_batch, _ = self.model(torch.cat(patches))
        self.setProgressBar(75)
        counter = 0
        self.setMessage('Post processing ')
        for clas_pred, bbox_pred, x, y in zip(class_pred_batch, bbox_pred_batch, x_coordinates, y_coordinates):

            bbox_pred, scores, preds = process_output(clas_pred, bbox_pred, self.anchors, detect_thresh=0.3)

            if bbox_pred is not None:
                to_keep = nms(bbox_pred, scores, 0.5)  # nms_thresh=
                bbox_pred, preds, scores = bbox_pred[to_keep].cpu(), preds[to_keep].cpu(), scores[to_keep].cpu()

                t_sz = torch.Tensor([self.shape, self.shape])[None].float()
                bbox_pred = rescale_box(bbox_pred, t_sz)

                for box, pred, score in zip(bbox_pred, preds, scores):
                    y_box, x_box = box[:2]
                    h, w = box[2:4]

                    x1 = int(x_box) * self.down_factor + x
                    y1 = int(y_box) * self.down_factor + y
                    x2 = x1 + int(w) * self.down_factor
                    y2 = y1 + int(h) * self.down_factor

                    self.annos_original.append([x1, y1, x2, y2, int(pred), float(score)])

                    myanno = annotations.rectangularAnnotation(uid=uid,
                                                               x1=x1, y1=y1,
                                                               x2=x2, y2=y2,
                                                               # text="conf: {:1.2}".format(score),
                                                               pluginAnnotationLabel=self.annotationLabels[
                                                                   int(pred)])

                    self.annos.append(myanno)

                    uid += 1

            self.setProgressBar((counter / (len(y_coordinates))) / 4 * 100 + 75)
            counter += 1
        self.performe_nms(nms_thresh)
        self.setMessage('Done')

    def performe_nms(self, nms_thresh):
        boxes = np.array(self.annos_original)
        if len(self.annos_original) > 0:
            ids = non_max_suppression_by_distance(boxes, boxes[:, 5], nms_thresh, True)

            self.annos_original = [self.annos_original[id] for id in ids]
            self.annos = [self.annos[id] for id in ids]

            self.updateAnnotations()

    def calculate_eiph_statistics(self, job, data_source):
        result_dict = {"Golde et al.": "nan", "Doucet et al.": "nan", "Total": 0, 0: 0, 1: 0, 2: 0, 3: 0, 4: 0, }
        total = 0

        grades = list()
        if len(self.annos_original) > 0 \
                and "INFERENCE" == data_source:
            boxes = np.array(self.annos_original)

            ids = (boxes[:, 0] > job.coordinates[0]) \
                  & (boxes[:, 1] > job.coordinates[1]) \
                  & (boxes[:, 2] < job.coordinates[0] + job.coordinates[2]) \
                  & (boxes[:, 3] < job.coordinates[1] + job.coordinates[3])

            grades = list(boxes[ids, 4])

        elif job.openedDatabase.dbOpened == True and "DATABASE" == data_source:
            grades = [self.classes[annotation.labels[0].classId] for id, annotation in job.openedDatabase.VA.items()
                      if annotation.labels[0].classId in self.classes]

        for grade in grades:
            total += 1
            result_dict[grade] += 1

        result_dict["Total"] = total
        if total > 0:
            result_dict["Golde et al."] = '{:f}'.format(np.mean(grades))
            # Doucet
            doucet = sum([(result_dict[i] / (total / 100)) * i for i in range(5)])
            result_dict["Doucet et al."] = '{:f}'.format(doucet)
        return result_dict

    def create_overlay(self, job, data_source):

        annotations = np.array([[0,0,0,0,0]])
        if "INFERENCE" == data_source:
            annotations = np.array(self.annos_original)

        elif "DATABASE" == data_source:
            annotations = np.array([[a[1].x1, a[1].y1, a[1].x1 + 2 * a[1].r, a[1].y1 + 2 * a[1].r,
                                     self.classes[a[1].labels[0].classId]] for a in
                                    job.openedDatabase.annotations.items()
                                    if a[1].labels[0].classId in self.classes])
            self.headmap_resolution = int(job.configuration["Headmap_Resolution"])

        x_steps = range(0, self.level_dimension[0] - 2 * self.headmap_resolution, int(self.headmap_resolution / 2))
        y_steps = range(0, self.level_dimension[1] - 2 * self.headmap_resolution, int(self.headmap_resolution / 2))
        gt_image = np.zeros(shape=(len(x_steps) + 1, len(y_steps) + 1))
        x_index = 0
        for x in x_steps:
            y_index = 0
            for y in y_steps:
                ids = ((annotations[:, 1]) > x) \
                      & ((annotations[:, 0]) > y) \
                      & ((annotations[:, 3]) < x + self.headmap_resolution) \
                      & ((annotations[:, 2]) < y + self.headmap_resolution)

                score = np.mean(annotations[ids, 4]) if np.count_nonzero(ids) > 1 else 0
                gt_image[x_index, y_index] = score

                y_index += 1
            x_index += 1
        gt_image = np.expand_dims(gt_image * (255. / 4), axis=2).astype(np.uint8)
        overlay = cv2.applyColorMap(gt_image, cv2.COLORMAP_JET)
        # Mask overlay
        overlay[np.array(gt_image == 0)[:, :, [0, 0, 0]]] = [255]

        return overlay

    def overlayHeatmap(self, numpyImage) -> np.ndarray:

        if self.overlay is not None:
            temp_overlay = cv2.resize(self.overlay, numpyImage.shape[:2])
            return cv2.addWeighted(numpyImage, 0.7, temp_overlay, 0.3, 0)
        else:
            return numpyImage


    def getAnnotations(self):
        return self.annos


    def getAnnotationLabels(self):
            # sending default annotation labels
            return [self.annotationLabels[k] for k in self.annotationLabels.keys()]


def rescale_box(bboxes, size: Tensor):
    bboxes[:, :2] = bboxes[:, :2] - bboxes[:, 2:] / 2
    bboxes[:, :2] = (bboxes[:, :2] + 1) * size / 2
    bboxes[:, 2:] = bboxes[:, 2:] * size / 2
    bboxes = bboxes.long()
    return bboxes