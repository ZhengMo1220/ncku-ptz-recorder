import os
from onvif import ONVIFCamera

from utils.app_config import get_camera_credentials

# 專案根目錄下的 wsdl/。onvif 0.2.12 預設找 site-packages/wsdl，但
# 0.2.12 的 setup.py 用過時的 data_files，pip 安裝時 WSDL 不會被放
# 到對的地方。我們自帶一份，路徑顯式指過去比較穩。
_WSDL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wsdl")


class ptzControl(object):
    def __init__(self, IP="10.30.3.34"):
        # Initialize the ONVIF camera connection
        self.mycam = ONVIFCamera(IP, 80, *get_camera_credentials(), wsdl_dir=_WSDL_DIR)
        self.media = self.mycam.create_media_service()
        self.ptz = self.mycam.create_ptz_service()
        self.media_profile = self.media.GetProfiles()[0]
        self.movement_step_size = 0.05

        # Get PTZ configuration options
        request = self.ptz.create_type('GetConfigurationOptions')
        request.ConfigurationToken = self.media_profile.PTZConfiguration.token
        ptz_configuration_options = self.ptz.GetConfigurationOptions(request)

        # Set up continuous move request
        self.requestc = self.ptz.create_type('ContinuousMove')
        self.requestc.ProfileToken = self.media_profile.token
        if self.requestc.Velocity is None:
            self.requestc.Velocity = self.ptz.GetStatus({'ProfileToken': self.media_profile.token}).Position
            self.requestc.Velocity.PanTilt.space = ptz_configuration_options.Spaces.ContinuousPanTiltVelocitySpace[0].URI
            self.requestc.Velocity.Zoom.space = ptz_configuration_options.Spaces.ContinuousZoomVelocitySpace[0].URI

        # Set up stop request
        self.requests = self.ptz.create_type('Stop')
        self.requests.ProfileToken = self.media_profile.token
        self.stop()

    def stop(self):
        # Stop all PTZ movements
        self.requests.PanTilt = True
        self.requests.Zoom = True
        self.ptz.Stop(self.requests)

    def move_up(self):
        # Move the camera up
        status = self.ptz.GetStatus({'ProfileToken': self.media_profile.token})
        pan = status.Position.PanTilt.x
        tilt = status.Position.PanTilt.y
        zoom = status.Position.Zoom.x

        # Ensure tilt doesn't exceed 1
        new_tilt = min(tilt + self.movement_step_size, 1.0)

        self.requesta = self.ptz.create_type('AbsoluteMove')
        self.requesta.ProfileToken = self.media_profile.token
        self.requesta.Position = status.Position
        self.requesta.Position.PanTilt.x = pan
        self.requesta.Position.PanTilt.y = new_tilt
        self.requesta.Position.Zoom.x = zoom
        self.ptz.AbsoluteMove(self.requesta)

    def move_down(self):
        # Move the camera down
        status = self.ptz.GetStatus({'ProfileToken': self.media_profile.token})
        pan = status.Position.PanTilt.x
        tilt = status.Position.PanTilt.y
        zoom = status.Position.Zoom.x

        # Ensure tilt doesn't go below -1
        new_tilt = max(tilt - self.movement_step_size, -1.0)

        self.requesta = self.ptz.create_type('AbsoluteMove')
        self.requesta.ProfileToken = self.media_profile.token
        self.requesta.Position = status.Position
        self.requesta.Position.PanTilt.x = pan
        self.requesta.Position.PanTilt.y = new_tilt
        self.requesta.Position.Zoom.x = zoom
        self.ptz.AbsoluteMove(self.requesta)

    def move_left(self):
        # Move the camera left
        status = self.ptz.GetStatus({'ProfileToken': self.media_profile.token})
        pan = status.Position.PanTilt.x
        tilt = status.Position.PanTilt.y
        zoom = status.Position.Zoom.x

        # Ensure pan doesn't go below -1
        new_pan = max(pan - self.movement_step_size, -1.0)

        self.requesta = self.ptz.create_type('AbsoluteMove')
        self.requesta.ProfileToken = self.media_profile.token
        self.requesta.Position = status.Position
        self.requesta.Position.PanTilt.x = new_pan
        self.requesta.Position.PanTilt.y = tilt
        self.requesta.Position.Zoom.x = zoom
        self.ptz.AbsoluteMove(self.requesta)

    def move_right(self):
        # Move the camera right
        status = self.ptz.GetStatus({'ProfileToken': self.media_profile.token})
        pan = status.Position.PanTilt.x
        tilt = status.Position.PanTilt.y
        zoom = status.Position.Zoom.x

        # Ensure pan doesn't exceed 1
        new_pan = min(pan + self.movement_step_size, 1.0)

        self.requesta = self.ptz.create_type('AbsoluteMove')
        self.requesta.ProfileToken = self.media_profile.token
        self.requesta.Position = status.Position
        self.requesta.Position.PanTilt.x = new_pan
        self.requesta.Position.PanTilt.y = tilt
        self.requesta.Position.Zoom.x = zoom
        self.ptz.AbsoluteMove(self.requesta)

    def zoom_in(self):
        # Zoom in the camera
        status = self.ptz.GetStatus({'ProfileToken': self.media_profile.token})
        pan = status.Position.PanTilt.x
        tilt = status.Position.PanTilt.y
        zoom = status.Position.Zoom.x

        # Ensure zoom doesn't exceed 1
        new_zoom = min(zoom + self.movement_step_size, 1.0)

        self.requesta = self.ptz.create_type('AbsoluteMove')
        self.requesta.ProfileToken = self.media_profile.token
        self.requesta.Position = status.Position
        self.requesta.Position.PanTilt.x = pan
        self.requesta.Position.PanTilt.y = tilt
        self.requesta.Position.Zoom.x = new_zoom
        self.ptz.AbsoluteMove(self.requesta)

    def zoom_out(self):
        # Zoom out the camera
        status = self.ptz.GetStatus({'ProfileToken': self.media_profile.token})
        pan = status.Position.PanTilt.x
        tilt = status.Position.PanTilt.y
        zoom = status.Position.Zoom.x

        # Ensure zoom doesn't go below 0
        new_zoom = max(zoom - self.movement_step_size, 0.0)

        self.requesta = self.ptz.create_type('AbsoluteMove')
        self.requesta.ProfileToken = self.media_profile.token
        self.requesta.Position = status.Position
        self.requesta.Position.PanTilt.x = pan
        self.requesta.Position.PanTilt.y = tilt
        self.requesta.Position.Zoom.x = new_zoom
        self.ptz.AbsoluteMove(self.requesta)