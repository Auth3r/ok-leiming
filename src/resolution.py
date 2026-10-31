"""One reference-space API over native client pixels; DPI is never a multiplier."""
from dataclasses import dataclass
import json
import cv2
import numpy as np
from ok import Box
from src.recognition_geometry import reference_bbox_to_client

REFERENCE = (3840, 2160)
AUTO = (0, 0)
SIZES = (AUTO, (1280, 720), REFERENCE)
OPTIONS = ('自动识别（默认）', '1280×720 窗口化', '3840×2160 无边框')


@dataclass(frozen=True)
class Resolution:
    size: tuple
    selected: tuple = AUTO

    def __post_init__(self):
        if self.size not in SIZES[1:]:
            raise ValueError(f'不支持的游戏客户区：{self.size}；需要1280×720或3840×2160')
        if self.selected not in SIZES or self.selected not in (AUTO, self.size):
            raise ValueError(f'手动分辨率{self.selected}与实际客户区{self.size}不符，拒绝输入')

    def point(self, x, y):
        if not 0 <= x < REFERENCE[0] or not 0 <= y < REFERENCE[1]:
            raise ValueError(f'参考坐标越界：{(x,y)}')
        return (min(self.size[0]-1, round(x*self.size[0]/REFERENCE[0])),
                min(self.size[1]-1, round(y*self.size[1]/REFERENCE[1])))

    def roi(self, values):
        return reference_bbox_to_client(values, self.size)

    def box(self, b):
        if b is None: return None
        sx, sy = REFERENCE[0]/self.size[0], REFERENCE[1]/self.size[1]
        return Box(round(b.x*sx), round(b.y*sy), round(b.width*sx), round(b.height*sy),
                   b.confidence, b.name)


def selected_size(settings):
    size = (settings['resolution_width'], settings['resolution_height'])
    if any(type(v) is not int for v in size):
        raise ValueError('运行尺寸必须为整数像素')
    if size not in SIZES:
        raise ValueError('运行分辨率请选择自动识别、1280×720或3840×2160')
    return size


class ReferenceTask:
    """Keep existing business/crop coordinates; match and OCR the original pixels.

    Native calibrated assets override scaled legacy assets. A reference image is
    exposed ONLY to old pixel/crop consumers; OCR/matching never read that resize.
    All input still passes through the existing live Win32 client/DPI boundary.
    """
    def __init__(self, task, settings):
        self.original = task
        self.selected = selected_size(settings)
        self.native_frame = None
        self.reference_frame = None
        self.emit = None
        self.native_templates = {}
        self.matched_native_thresholds = {}
        self.capture = None
        self.capture_key = None
        self.metadata = None
        self.observed_revision = None
        from src.app_paths import APP_ROOT as ROOT
        path = ROOT/'templates/navigation_720p/manifest.json'
        if path.exists():
            for name, spec in json.loads(path.read_text(encoding='utf-8'))['templates'].items():
                target = spec.get('target', name)
                im = cv2.imdecode(np.fromfile(path.parent/spec['file'],dtype=np.uint8),1)
                self.native_templates.setdefault(target, []).append((im,spec.get('threshold',0)))
        path = ROOT/'templates/daily_native/manifest.json'
        if path.exists():
            for name,spec in json.loads(path.read_text(encoding='utf-8'))['templates'].items():
                im=cv2.imdecode(np.fromfile(path.parent/spec['file'],dtype=np.uint8),1)
                self.native_templates.setdefault(spec.get('target',name),[]).append((im,spec.get('threshold',0)))

    def __getattr__(self, name): return getattr(self.original, name)

    @property
    def frame(self):
        if self.reference_frame is None:
            return self.original.frame
        return self.reference_frame

    @property
    def geometry(self):
        frame = self.native_frame if self.native_frame is not None else self.original.frame
        return Resolution(tuple(frame.shape[1::-1]), self.selected)

    def next_frame(self):
        control=getattr(self.original,'task_control',None) or getattr(self.original,'roguelike_control',None)
        revision=control.revision if control else None
        manager = getattr(getattr(self.original,'executor',None),'device_manager',None)
        hwnd = getattr(getattr(manager,'hwnd_window',None),'hwnd',None)
        if hwnd:
            from src.client_geometry import validate_client_geometry
            size = validate_client_geometry(hwnd).size
            Resolution(size,self.selected)
        else: size = None
        if size in ((1280,720),REFERENCE):
            # Reuse the tested private-MTA native WGC lifecycle, including fresh
            # request timestamps, rather than the old framework's STA capture.
            from src.capture_720p import Capture720p
            import win32process
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            key=(int(hwnd),int(pid),size)
            if self.capture is not None and self.capture_key!=key:self.close()
            if self.capture is None:
                self.capture = Capture720p(hwnd,pid) if size==(1280,720) else Capture720p(hwnd,pid,client_size=size)
                self.capture_key=key
            packet = self.capture.get_frame()
            frame = packet.image
            self.metadata = packet.metadata
        else:
            # Explicit offline tasks have no HWND and no auditable live packet.
            # Never reuse provenance from a previous capture or a resized window.
            self.metadata=None
            frame = self.original.next_frame()
        if frame is None: return None
        try:
            geometry = Resolution(tuple(frame.shape[1::-1]), self.selected)
        except ValueError as error:
            from src.workflow import SafetyStop
            raise SafetyStop(str(error)) from error
        self.native_frame = frame
        self.reference_frame = frame if geometry.size == REFERENCE else cv2.resize(frame, REFERENCE)
        self.observed_revision=revision
        return self.reference_frame

    def _match(self, name, many, **kwargs):
        geometry = self.geometry
        if geometry.size == REFERENCE:
            kwargs.setdefault('frame',self.native_frame if self.native_frame is not None else self.original.frame)
            return getattr(self.original, 'find_feature' if many else 'find_one')(name, **kwargs)
        box = kwargs.get('box')
        if box:
            l,t,r,b = geometry.roi((round(box.x),round(box.y),round(box.x+box.width),round(box.y+box.height)))
            kwargs['box'] = Box(l,t,r-l,b-t)
        supplied = kwargs['template']
        templates = self.native_templates.get(name) or [(cv2.resize(supplied,
            (max(1,round(supplied.shape[1]/3)),max(1,round(supplied.shape[0]/3))), interpolation=cv2.INTER_AREA),0)]
        kwargs['frame'] = self.native_frame if self.native_frame is not None else self.original.frame
        found = []
        for template,minimum in templates:
            result = getattr(self.original, 'find_feature' if many else 'find_one')(name, **(kwargs|{'template':template}))
            # These calibrated daily assets have recorded desktop edge
            # rasterization mismatches. Other assets keep their existing path.
            if (name in ('seasonal_activity_anchor','dispatch_assigned','dark_map_landmark','gift')
                    and not many and minimum>0
                    and (result is None or result.confidence<minimum)):
                normalized=self.original.find_one(name, **(kwargs|{
                    'template':cv2.GaussianBlur(template,(5,5),1.0),
                    'frame':cv2.GaussianBlur(kwargs['frame'],(5,5),1.0)}))
                if normalized is not None and (result is None or normalized.confidence>result.confidence):
                    result=normalized
            found.extend((b,minimum) for b in (result if many else [result] if result else []))
        if many:
            unique = {(round(b.x),round(b.y)): b for b,minimum in found if b.confidence>=minimum}
            return [geometry.box(b) for b in unique.values()]
        if not found:return None
        valid=[(b,m) for b,m in found if b.confidence>=m]
        best,minimum=max(valid or found,key=lambda item:item[0].confidence)
        self.matched_native_thresholds[name]=minimum
        return geometry.box(best)

    def find_one(self, name, **kwargs): return self._match(name, False, **kwargs)
    def find_feature(self, name, **kwargs): return self._match(name, True, **kwargs)

    def ocr(self, **kwargs):
        # Upstream uses normalized ROI; retain it and return reference-space boxes.
        geometry = self.geometry
        if geometry.size == REFERENCE:
            kwargs.setdefault('frame',self.native_frame if self.native_frame is not None else self.original.frame)
            return self.original.ocr(**kwargs)
        requested = kwargs.get('frame',self.frame)
        width,height = requested.shape[1::-1]
        # Crafting has a deliberate 2048x1152 OCR reference frame. Preserve its
        # returned box space while reading the same full native client pixels.
        if (width,height) in (REFERENCE,(2048,1152),(1920,1080),geometry.size):
            kwargs['frame'] = self.native_frame if self.native_frame is not None else self.original.frame
            result = self.original.ocr(**kwargs)
            sx,sy = width/geometry.size[0],height/geometry.size[1]
            return [Box(round(b.x*sx),round(b.y*sy),round(b.width*sx),round(b.height*sy),b.confidence,b.name) for b in result]
        return self.original.ocr(**kwargs)  # Explicit cropped local-image OCR.

    def close(self):
        if self.capture is not None:
            self.capture.close()
            self.capture = None
            self.capture_key = None

    def _input(self, method, x, y, *args, **kwargs):
        # Re-read HWND size immediately before dispatch; moved windows are handled
        # by existing client_input_to_screen, changed sizes invalidate old targets.
        geometry = self.geometry
        manager = getattr(getattr(self.original,'executor',None),'device_manager',None)
        hwnd = getattr(getattr(manager,'hwnd_window',None),'hwnd',None)
        screen = None
        if hwnd:
            from src.client_geometry import validate_client_geometry, client_input_to_screen
            validate_client_geometry(hwnd,expected_size=geometry.size)
        point = geometry.point(x,y)
        if hwnd: screen = client_input_to_screen(hwnd,*point)
        if self.emit: self.emit('coordinate_transform',reference=[x,y],client=list(point),screen=screen,client_size=geometry.size)
        # The guard covers only dispatch and its cache reset. Framework sleep
        # can wait for a pause, so it must never hold the same lifecycle lock
        # that Pause/Stop need in order to wake or cancel that wait.
        after_sleep=kwargs.pop('after_sleep',0) if method in ('click','roguelike_click') else 0
        callback=lambda:getattr(self.original,method)(*point,*args,**kwargs)
        control=getattr(self.original,'task_control',None) or getattr(self.original,'roguelike_control',None)
        result=control.input(self.observed_revision,callback) if control else callback()
        if after_sleep>0:
            if control:
                checkpoint=getattr(self.original,'task_checkpoint',None) or control.checkpoint
                end=control.clock()+after_sleep
                while control.clock()<end:
                    checkpoint()
                    self.original.sleep(min(.1,max(0,end-control.clock())))
                checkpoint()
            else:self.original.sleep(after_sleep)
        return result

    def click(self, x, y, **kwargs): return self._input('click',x,y,**kwargs)
    def roguelike_click(self, x, y, **kwargs):
        method='roguelike_click' if hasattr(self.original,'roguelike_click') else 'click'
        return self._input(method,x,y,**kwargs)
    def move(self, x, y): return self._input('move',x,y)
    def scroll(self, x, y, count): return self._input('scroll',x,y,count)

    def swipe(self,x1,y1,x2,y2,duration=300):
        geometry=self.geometry
        start,end=geometry.point(x1,y1),geometry.point(x2,y2)
        manager=getattr(getattr(self.original,'executor',None),'device_manager',None)
        hwnd=getattr(getattr(manager,'hwnd_window',None),'hwnd',None)
        screens=None
        if hwnd:
            from src.client_geometry import validate_client_geometry,client_input_to_screen
            validate_client_geometry(hwnd,expected_size=geometry.size)
            screens=[client_input_to_screen(hwnd,*point) for point in (start,end)]
        if self.emit:self.emit('coordinate_transform',reference_segment=[[x1,y1],[x2,y2]],
            client_segment=[list(start),list(end)],screen_segment=screens,client_size=geometry.size)
        callback=lambda:self.original.swipe(*start,*end,duration=duration)
        control=getattr(self.original,'task_control',None) or getattr(self.original,'roguelike_control',None)
        return control.input(self.observed_revision,callback) if control else callback()
