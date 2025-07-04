import json
import os
import random
from typing import Iterable, List, Tuple, Union

import numpy as np
from avstack import calibration, sensors
from avstack.datastructs import DataContainer
from avstack.environment.objects import (
    ObjectState,
    ObjectStateDecoder,
    Occlusion,
    VehicleState,
)
from avstack.geometry import (
    Acceleration,
    AngularVelocity,
    Attitude,
    Box3D,
    GlobalOrigin3D,
    PointMatrix3D,
    Position,
    ReferenceFrame,
    Velocity,
    q_cam_to_stan,
)
from avstack.geometry import transformations as tforms
from avstack.modules.control import VehicleControlSignal
from avstack.modules.tracking.tracks import TrackContainerDecoder


def wrap_minus_pi_to_pi(phases):
    phases = (phases + np.pi) % (2 * np.pi) - np.pi
    return phases


def get_reference_from_line(line):
    """A bit of a hack"""
    line = line.split()
    assert line[0] == "origin", line[0]
    x = np.array([float(l) for l in line[1:4]])
    q = np.quaternion(*[float(l) for l in line[4:8]])
    return ReferenceFrame(x=x, q=q, reference=GlobalOrigin3D)


class BaseSceneManager:
    def __iter__(self) -> Iterable["BaseSceneDataset"]:
        for scene in self.scenes:
            yield self.get_scene_dataset_by_name(scene)

    def __len__(self):
        return len(self.scenes)

    def __getitem__(self, index):
        return self.get_scene_dataset_by_index(index)

    @property
    def name(self):
        return self.NAME

    def list_scenes(self):
        print(self.scenes)

    def get_splits_scenes(self):
        return self.splits_scenes

    def make_splits_scenes(self, seed=1, frac_train=0.7, frac_val=0.3, frac_test=0.0):
        """Split the scenes by hashing the experiment name and modding
        3:1 split using mod 4
        """
        rng = random.Random(seed)

        if not (frac_train + frac_val + frac_test) == 1.0:
            raise ValueError("Fractions must add to 1.0")

        # first two we alternate just to have one
        splits_scenes = {"train": [], "val": [], "test": []}
        for i, scene in enumerate(self.scenes):
            if i == 0:
                splits_scenes["train"].append(scene)
            elif i == 1:
                splits_scenes["val"].append(scene)
            else:
                rv = rng.random()
                if rv < frac_train:
                    splits_scenes["train"].append(scene)
                elif rv < (frac_train + frac_val):
                    splits_scenes["val"].append(scene)
                else:
                    splits_scenes["test"].append(scene)
        return splits_scenes

    def get_scene_dataset_by_name(self):
        raise NotImplementedError

    def get_scene_dataset_by_index(self):
        raise NotImplementedError


class BaseSceneDataset:
    sensor_IDs = {}

    def __init__(self, whitelist_types, ignore_types):
        self.whitelist_types = whitelist_types
        self.ignore_types = ignore_types
        self._frame_to_timestamp = {}

    def __str__(self):
        return f"{self.NAME} dataset of folder: {self.split_path}"

    def __repr__(self):
        return self.__str__()

    def __len__(self):
        return len(self.frames)

    @property
    def name(self):
        return self.NAME

    @property
    def frames(self):
        return self._frames

    @frames.setter
    def frames(self, frames):
        self._frames = frames

    def check_frame(self, frame):
        assert (
            frame in self.frames
        ), f"Candidate frame, {frame}, not in frame set {self.frames}"

    def get_agents(self, frame: int) -> "DataContainer":
        return self._load_agents(frame)

    def get_agent(self, frame: int, agent: int) -> VehicleState:
        agents = self.get_agents(frame)
        return [ag for ag in agents if ag.ID == agent][0]

    def get_agent_set(self, frame: int) -> set:
        return self._load_agent_set(frame=frame)

    def get_sensor_ID(self, sensor, agent=None) -> int:
        try:
            return self.sensor_IDs[sensor]
        except KeyError:
            try:
                return self.sensor_IDs[self.sensors[sensor]]
            except (AttributeError, KeyError):
                try:
                    return self.sensor_IDs[agent][sensor]
                except KeyError as e:
                    raise e

    def get_sensor_name(self, sensor, agent=None) -> str:
        if (sensor is None) or (sensor not in self.sensors):
            return sensor
        else:
            return self.sensors[sensor]

    def get_sensor_names_by_type(self, sensor_type: str, agent=None) -> List[str]:
        return self._load_sensor_names_by_type(sensor_type=sensor_type, agent=agent)

    def get_frames(self, sensor, agent=None) -> List[int]:
        sensor = self.get_sensor_name(sensor, agent=agent)
        return self._load_frames(sensor=sensor, agent=agent)

    def get_timestamps(self, sensor, agent=None, utime=False) -> List[float]:
        sensor = self.get_sensor_name(sensor, agent=agent)
        return self._load_timestamps(sensor=sensor, agent=agent, utime=utime)

    def get_calibration(self, frame, sensor, agent=None) -> calibration.Calibration:
        sensor = self.get_sensor_name(sensor, agent)
        ego_reference = self.get_ego_reference(frame)
        return self._load_calibration(
            frame, agent=agent, sensor=sensor, ego_reference=ego_reference
        )

    def get_control_signal(
        self, frame, agent=None, bounded=False
    ) -> VehicleControlSignal:
        return self._load_control_signal(frame, agent=agent, bounded=bounded)

    def get_ego(self, frame, agent=None) -> ObjectState:
        return self._load_ego(frame)

    def get_ego_reference(self, frame, agent=None) -> ReferenceFrame:
        return self.get_ego(frame).as_reference()

    def get_sensor_data_filepath(self, frame, sensor, agent=None):
        sensor = self.get_sensor_name(sensor=sensor, agent=agent)
        return self._load_sensor_data_filepath(frame, sensor=sensor, agent=agent)

    def get_image(self, frame, sensor=None, agent=None) -> sensors.ImageData:
        sensor = self.get_sensor_name(sensor, agent=agent)
        ts = self.get_timestamp(frame, sensor, agent=agent)
        data = self._load_image(frame, sensor=sensor, agent=agent)
        cam_string = "image-%i" % sensor if isinstance(sensor, int) else sensor
        cam_string = (
            cam_string if sensor is not None else self.get_sensor_name("main_camera")
        )
        calib = self.get_calibration(frame, sensor=cam_string, agent=agent)
        return sensors.ImageData(
            ts,
            frame,
            data,
            calib,
            self.get_sensor_ID(cam_string, agent),
            channel_order="rgb",
        )

    def get_semseg_image(
        self, frame, sensor=None, agent=None
    ) -> sensors.SemanticSegmentationImageData:
        if sensor is None:
            sensor = self.sensors["semseg"]
        sensor = self.get_sensor_name(sensor, agent)
        ts = self.get_timestamp(frame, sensor=sensor, agent=agent)
        data = self._load_semseg_image(frame, sensor=sensor, agent=agent)
        cam_string = "image-%i" % sensor if isinstance(sensor, int) else sensor
        calib = self.get_calibration(frame, cam_string)
        return sensors.SemanticSegmentationImageData(
            ts, frame, data, calib, self.get_sensor_ID(cam_string, agent)
        )

    def get_depth_image(self, frame, sensor=None, agent=None) -> sensors.DepthImageData:
        if sensor is None:
            sensor = self.sensors["depth"]
        sensor = self.get_sensor_name(sensor, agent)
        ts = self.get_timestamp(frame, sensor=sensor, agent=agent)
        data = self._load_depth_image(frame, sensor=sensor, agent=agent)
        cam_string = "image-%i" % sensor if isinstance(sensor, int) else sensor
        calib = self.get_calibration(frame, sensor=cam_string, agent=agent)
        return sensors.DepthImageData(
            ts, frame, data, calib, self.get_sensor_ID(cam_string, agent)
        )

    def get_lidar(
        self,
        frame,
        sensor=None,
        agent=None,
        filter_front=False,
        min_range=None,
        max_range=None,
        with_panoptic=False,
    ) -> sensors.LidarData:
        if sensor is None:
            sensor = self.sensors["lidar"]
        sensor = self.get_sensor_name(sensor, agent)
        ts = self.get_timestamp(frame, sensor, agent=agent)
        calib = self.get_calibration(frame, sensor, agent=agent)
        data = self._load_lidar(
            frame,
            sensor=sensor,
            agent=agent,
            filter_front=filter_front,
            with_panoptic=with_panoptic,
        )
        data = PointMatrix3D(data, calib)
        pc = sensors.LidarData(
            ts, frame, data, calib, self.get_sensor_ID(sensor, agent=agent)
        )
        if (min_range is not None) or (max_range is not None):
            pc.filter_by_range(min_range, max_range, inplace=True)
        return pc

    def get_radar(
        self,
        frame,
        sensor=None,
        agent=None,
        min_range=None,
        max_range=None,
    ) -> sensors.RadarDataRazelRRT:
        if sensor is None:
            sensor = self.sensors["radar"]
        sensor = self.get_sensor_name(sensor, agent)
        ts = self.get_timestamp(frame, sensor)
        calib = self.get_calibration(frame, sensor)
        data = self._load_radar(frame, sensor)  # razelrrt data
        data = PointMatrix3D(data, calib)
        rad = sensors.RadarDataRazelRRT(
            ts, frame, data, calib, self.get_sensor_ID(sensor)
        )
        if (min_range is not None) or (max_range is not None):
            rad.filter_by_range(min_range, max_range, inplace=True)
        return rad

    def get_objects(
        self,
        frame,
        sensor="main_camera",
        agent=None,
        max_dist=None,
        max_occ=None,
        in_global: bool = False,
        **kwargs,
    ) -> DataContainer:
        reference = self.get_ego_reference(frame, agent=agent)
        sensor = self.get_sensor_name(sensor, agent=agent)
        objs = self._load_objects(frame, sensor=sensor, agent=agent, **kwargs)
        timestamp = self.get_timestamp(frame=frame, sensor=sensor, agent=agent)
        if max_occ is not None:
            objs = [
                obj
                for obj in objs
                if (obj.occlusion <= max_occ) or (obj.occlusion == Occlusion.UNKNOWN)
            ]
        if max_dist is not None:
            if sensor == "ego":
                calib = calibration.Calibration(reference)
            else:
                calib = self.get_calibration(frame, sensor, agent=agent)
            objs = [
                obj for obj in objs if obj.position.distance(calib.reference) < max_dist
            ]
        objs = DataContainer(
            source_identifier=sensor, frame=frame, timestamp=timestamp, data=objs
        )
        if in_global:
            objs = objs.apply_and_return(
                "change_reference", GlobalOrigin3D, inplace=False
            )
        return objs

    def get_objects_global(
        self,
        frame,
        max_dist: Union[Tuple[ReferenceFrame, float], None] = None,
        **kwargs,
    ) -> DataContainer:
        return self._load_objects_global(frame, max_dist=max_dist, **kwargs)

    def get_number_of_objects(self, frame, **kwargs) -> int:
        return self._number_objects_from_file(frame, **kwargs)

    def get_objects_from_file(self, fname, whitelist_types, max_dist=None):
        return self._load_objects_from_file(fname, whitelist_types, max_dist=max_dist)

    def get_timestamp(self, frame, sensor=None, agent=None, utime=False) -> float:
        sensor = self.get_sensor_name(sensor, agent=agent)
        return self._load_timestamp(frame, sensor=sensor, agent=agent, utime=utime)

    def get_frame_at_timestamp(
        self, timestamp, sensor=None, agent=None, utime=False, dt_tolerance: float = 0.5
    ) -> int:
        # get the closest timestamp in the set
        timestamps = self.get_timestamps(sensor=sensor, agent=agent, utime=utime)
        idx_frame = np.argmin(abs(np.array(timestamps) - timestamp))
        frame = self.get_frames(sensor=sensor, agent=agent)[idx_frame]

        # handle potential error
        dt_real = timestamps[idx_frame] - timestamp
        if abs(dt_real) > dt_tolerance:
            raise RuntimeError(
                f"Query time not within tolerance ({dt_tolerance}) of any timestamps, dt is {dt_real:.2f}"
            )

        return frame

    def get_sensor_file_path(self, frame, sensor, agent=None):
        return self._get_sensor_file_name(frame, sensor)

    def save_calibration(self, frame, calib, folder, **kwargs):
        if not os.path.exists(folder):
            os.makedirs(folder)
        self._save_calibration(frame, calib, folder, **kwargs)

    def save_objects(self, frame, objects, folder, file=None):
        if not os.path.exists(folder):
            os.makedirs(folder)
        self._save_objects(frame, objects, folder, file=file)

    def _load_sensor_names_by_type(self, sensor_type, agent):
        raise NotImplementedError

    def _load_agents(self, frame):
        raise NotImplementedError

    def _load_vehicle_control_signal(self, frame, agent=None):
        raise NotImplementedError

    def _load_ego(self, frame, agent=None):
        raise NotImplementedError

    def _load_control_signal(self, frame, agent, bounded):
        raise NotImplementedError

    def _load_frames(self, sensor):
        raise NotImplementedError

    def _load_calibration(self, frame, sensor, reference):
        raise NotImplementedError

    def _load_image(self, frame, sensor, agent=None):
        raise NotImplementedError

    def _load_semseg_image(self, frame, sensor, agent=None):
        raise NotImplementedError

    def _load_depth_image(self, frame, sensor, agent=None):
        raise NotImplementedError

    def _load_lidar(self, frame, sensor, agent=None):
        raise NotImplementedError

    def _load_objects(self, frame, sensor, agent=None):
        raise NotImplementedError

    def _load_sensor_data_filepath(self, frame, sensor: str, agent=None):
        return self._get_sensor_file_name(frame, sensor, agent=agent)

    def _load_timestamps(self, sensor, agent, utime):
        """This is slightly slow, so rely on subclass if faster way exists"""
        if sensor is not None:
            # build up a dictionary of frame --> timestamps
            agent_index = "default" if agent is None else agent
            if agent_index not in self._frame_to_timestamp:
                self._frame_to_timestamp[agent_index] = {}
            if sensor not in self._frame_to_timestamp[agent_index]:
                frames = self.get_frames(sensor=sensor, agent=agent)
                self._frame_to_timestamp[agent_index][sensor] = {
                    frame: self.get_timestamp(
                        frame=frame,
                        sensor=sensor,
                        agent=agent,
                        utime=utime,
                    )
                    for frame in frames
                }

            # get the timestamps array
            timestamps = list(self._frame_to_timestamp[agent_index][sensor].values())
        else:
            raise NotImplementedError("TODO")
        return timestamps

    def _load_objects_from_file(
        self,
        fname,
        whitelist_types=None,
        ignore_types=None,
        max_dist=None,
        dist_ref=GlobalOrigin3D,
    ):
        # -- prep whitelist types
        if whitelist_types is None:
            whitelist_types = self.nominal_whitelist_types
        if not isinstance(whitelist_types, list):
            whitelist_types = [whitelist_types]
        whitelist_types = [wh.lower() for wh in whitelist_types]
        # -- prep ignore types
        if ignore_types is None:
            ignore_types = self.nominal_ignore_types
        if not isinstance(ignore_types, list):
            ignore_types = [ignore_types]
        ignore_types = [ig.lower() for ig in ignore_types]
        # -- read objects
        with open(fname, "r") as f:
            lines = [line.rstrip() for line in f.readlines()]
        objects = []
        for line in lines:
            if "datacontainer" in line:
                dc = json.loads(line, cls=TrackContainerDecoder)
                objects = dc.data
                break
            else:
                obj = self.parse_label_line(line)
            # -- type filter
            if ("all" in whitelist_types) or (obj.obj_type.lower() in whitelist_types):
                if obj.obj_type.lower() in ignore_types:
                    continue
            else:
                continue
            # -- distance filter
            if max_dist is not None:
                obj.change_reference(dist_ref, inplace=True)
                if obj.position.norm() > max_dist:
                    continue
            # -- save if made to here
            objects.append(obj)

        return np.array(objects)

    def _load_timestamp(self, frame):
        raise NotImplementedError

    def _save_objects(self, frame, objects, folder):
        raise NotImplementedError

    @staticmethod
    def read_dict_text_file(filepath):
        """Read in a calibration file and parse into a dictionary.
        Ref: https://github.com/utiasSTARS/pykitti/blob/master/pykitti/utils.py
        """
        data = {}
        with open(filepath, "r") as f:
            for line in f.readlines():
                line = line.rstrip()
                if len(line) == 0:
                    continue
                key, value = line.split(":", 1)
                # The only non-float values in these files are dates, which
                # we don't care about anyway
                try:
                    data[key] = np.array([float(x) for x in value.split()])
                except ValueError:
                    pass
        return data

    def parse_label_line(self, label_file_line):
        # Parse data elements
        data = label_file_line.strip("\n").split(" ")
        if data[0] == "nuscenes":
            idx = 2
            ts = data[idx]
            idx += 1
            ID = data[idx]
            idx += 1
            obj_type = data[idx]
            idx += 1
            occ = Occlusion(int(data[idx]))
            idx += 1
            pos = data[idx : (idx + 3)]
            idx += 3
            t_box = pos
            vel = data[idx : (idx + 3)]
            idx += 3
            if np.all([v == "None" for v in vel]):
                vel = None
            acc = data[idx : (idx + 3)]
            idx += 3
            if np.all([a == "None" for a in acc]):
                acc = None
            box_size = data[idx : (idx + 3)]
            idx += 3
            q_O_to_obj = np.quaternion(*[float(d) for d in data[idx : (idx + 4)]])
            idx += 4
            if data[0] == "nuscenes":
                q_O_to_obj = q_O_to_obj.conjugate()
            ang = None
            where_is_t = data[idx]
            idx += 1
            object_reference = get_reference_from_line(" ".join(data[idx:]))
        elif data[0] == "kitti-v2":  # converted kitti raw data -- longitudinal dataset
            idx = 1
            ts = data[idx]
            idx += 1
            ID = data[idx]
            idx += 1
            obj_type = data[idx]
            idx += 1
            orientation = data[idx]
            idx += 1
            occ = Occlusion.UNKNOWN
            box2d = data[idx : (idx + 4)]
            idx += 4
            box_size = data[idx : (idx + 3)]
            idx += 3
            t_box = data[idx : (idx + 3)]
            idx += 3
            yaw = float(data[idx])
            idx += 1
            score = float(data[idx])
            idx += 1
            object_reference = get_reference_from_line(" ".join(data[idx:]))
            pos = t_box
            vel = acc = ang = None
            where_is_t = "bottom"
            q_V_to_obj = tforms.transform_orientation([0, 0, yaw], "euler", "quat")
            q_O_to_V = object_reference.q.conjugate()
            q_O_to_obj = q_V_to_obj * q_O_to_V
        elif label_file_line[0] == "{":
            obj = json.loads(label_file_line, cls=ObjectStateDecoder)
            return obj
        else:  # data[0] == "kitti":  # assume kitti with no prefix -- this is for kitti static dataset
            ts = 0.0
            # not ideal but ensures unique IDs
            ID = np.random.randint(low=0, high=1e6)
            obj_type = data[0]
            occ = Occlusion.UNKNOWN
            box2d = data[4:8]
            t_box = data[11:14]  # x_C_2_Obj == x_O_2_Obj for O as camera
            where_is_t = "bottom"
            box_size = data[8:11]
            pos = t_box
            vel = acc = ang = None
            try:
                yaw = -float(data[14]) - np.pi / 2
            except ValueError:
                import pdb

                pdb.set_trace()
            object_reference = self.get_calibration(self.frames[0], "image-2").reference
            q_Ccam_to_Cstan = q_cam_to_stan
            q_Cstan_to_obj = tforms.transform_orientation([0, 0, yaw], "euler", "quat")
            q_O_to_obj = q_Cstan_to_obj * q_Ccam_to_Cstan
        try:
            ID = int(ID)
        except ValueError as e:
            pass
        pos = Position(np.array([float(p) for p in pos]), object_reference)
        vel = (
            Velocity(np.array([float(v) for v in vel]), object_reference)
            if vel is not None
            else None
        )
        acc = (
            Acceleration(np.array([float(a) for a in acc]), object_reference)
            if acc is not None
            else None
        )
        rot = Attitude(q_O_to_obj, object_reference)
        ang = (
            AngularVelocity(np.quaternion([float(a) for a in ang]), object_reference)
            if ang is not None
            else None
        )
        hwl = [float(b) for b in box_size]
        box3d = Box3D(pos, rot, hwl, obj_type=obj_type, where_is_t=where_is_t, ID=ID)
        obj = VehicleState(obj_type, ID)
        obj.set(
            t=float(ts),
            position=pos,
            box=box3d,
            velocity=vel,
            acceleration=acc,
            attitude=rot,
            angular_velocity=ang,
            occlusion=occ,
        )
        return obj
