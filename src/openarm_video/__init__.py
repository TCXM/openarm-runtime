"""Reusable RGB/depth capture; imports neither open hardware nor load GUI/SDKs."""

from .camera import CameraConfig, Frame, OpenCVCamera
from .capture import CameraReader, VideoCapture
from .depth import OpenNIDepthCamera, build_depth_helper
from .profile import load_cameras
from .stream import create_app, encode_frame, serve

__all__ = ["CameraConfig", "Frame", "OpenCVCamera", "OpenNIDepthCamera",
           "CameraReader", "VideoCapture", "load_cameras", "build_depth_helper",
           "create_app", "encode_frame", "serve"]
