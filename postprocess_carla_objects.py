# -*- coding: utf-8 -*-
# @Author: Spencer H
# @Date:   2022-09-05
# @Last Modified by:   Spencer H
# @Last Modified date: 2022-10-22
# @Description:
"""
Take objects in the global frame and create their local
representations in each of the sensors
"""

import os
import argparse
from tqdm import tqdm
from multiprocessing import Pool
from functools import partial
import logging

import avstack
import avapi

from avstack.environment.objects import Occlusion


def main(args, frame_start=4, frame_end_trim=4, n_frames_max=10000):
    CSM = avapi.carla.CarlaScenesManager(args.data_dir)
    print("Postprocessing carla dataset from {}{}".format(args.data_dir, "" if not args.multi else " with multiprocessing"))
    for i_scene, CDM in enumerate(CSM):
        print("Scene {} of {}".format(i_scene+1, len(CSM)))
        with_multi = args.multi
        chunksize = 10
        frames = [f for f in list(CDM.ego_frame_to_ts.keys()) if f >= frame_start]
        frames = frames[:max(1, min(n_frames_max, len(frames))-frame_end_trim)]
        frames_all = frames
        egos = {i:CDM.get_ego(i) for i in frames}
        nproc = max(1, min(100, int(len(frames)/chunksize)))
        if with_multi:
            print('Getting global objects from all frames')
            with Pool(nproc) as p:
                part_func = partial(get_obj_glob_by_frames, CDM)
                objects_global = dict(zip(frames, tqdm(p.imap(part_func,
                    frames, chunksize=chunksize), position=0, leave=True, total=len(frames))))
        else:
            print('Getting global objects from all frames')
            objects_global = {i_frame:get_obj_glob_by_frames(CDM, i_frame) for i_frame in tqdm(frames)}

        assert len(objects_global) == len(frames), "{} {}".format(len(objects_global, len(frames)))
        print('Putting objects into ego frame')
        process_func_sensors(CDM, 'ego', egos, objects_global, frames, args.data_dir, with_multi=args.multi)

        print('Putting objects into sensor frames')
        for i_sens, (sens, frames) in enumerate(CDM.sensor_frames.items()):
            print('Processing {} of {} - sensor {}'.format(i_sens+1, len(CDM.sensor_frames), sens))
            frames_this = [frame for frame in frames if frame in frames_all]
            egos_this = {frame:egos[frame] for frame in frames_this}
            objects_global_this = {frame:objects_global[frame] for frame in frames_this}
            process_func_sensors(CDM, sens, egos_this, objects_global_this, frames_this, args.data_dir, with_multi=args.multi)


def get_obj_glob_by_frames(CDM, i_frame):
    return CDM.get_objects_global(i_frame)


def process_func_sensors(CDM, sens, egos, objects_global, frames, data_dir, with_multi):
    """
    Post-process frames for a sensor
    """
    assert len(egos) == len(objects_global) == len(frames), '{}, {}, {}'.format(len(egos), len(objects_global), len(frames))
    obj_sens_folder = os.path.join(data_dir, CDM.scene, 'objects_sensor', sens)
    os.makedirs(obj_sens_folder, exist_ok=True)
    func = partial(process_func_frames, CDM, sens, obj_sens_folder)

    chunksize = 20
    nproc = max(1, min(20, int(len(frames)/chunksize)))
    if with_multi:
        with Pool(nproc) as p:
            res = list(tqdm(p.istarmap(func,
                zip(egos.values(), objects_global.values(), frames), chunksize=chunksize),
                position=0, leave=True, total=len(frames)))
    else:
        for i_frame in tqdm(frames):
            func(egos[i_frame], objects_global[i_frame], i_frame)


def process_func_frames(CDM, sens, obj_sens_folder, ego, objects_global, i_frame):
    ego_ref = ego.as_reference()
    if sens == "ego":
        objects_local = objects_global
        for obj in objects_local:
            obj.change_reference(ego_ref, inplace=True)
    else:
        try:
            calib = CDM.get_calibration(i_frame, sens)
        except FileNotFoundError as e:
            if i_frame > 10:
                return  # probably just because we stopped early (?)
            else:
                raise e

        # -- add ego object to objects if other sensor (e.g., infra)
        if ego.position.distance(calib.reference) > 5:
            objects_global.append(ego)

        # -- change to sensor origin
        objects_local = objects_global
        for obj in objects_local:
            obj.change_reference(calib.reference, inplace=True)

        # -- filter in view of cameras
        if 'cam' in sens.lower():
            objects_local = [obj for obj in objects_local if
                avstack.maskfilters.box_in_fov(obj.box, calib,
                    d_thresh=150, check_reference=False)]

        # -- get depth image
        check_reference = False
        if 'CAM' in sens:
            if 'DEPTH' not in sens:
                sens_d = 'DEPTH' + sens
            else:
                sens_d = sens
            try:
                d_img = CDM.get_depthimage(i_frame, sens_d)
            except Exception as e:
                d_img = None
                try:
                    if 'infra' in sens.lower():
                        pc = CDM.get_lidar(i_frame, sens.replace('CAM', 'LIDAR'))  # hack this for now....
                    else:
                        pc = CDM.get_lidar(i_frame, 'LIDAR_TOP')  # hack this for now....
                    check_reference = True
                except Exception as e:
                    logging.warning(e)
                    pc = None
                    print('Could not load depth image...setting occlusion as UNKNOWN')
        elif 'LIDAR' in sens:
            d_img = None
            pc = CDM.get_lidar(i_frame, sens)
        else:
            raise NotImplementedError(sens)

        # -- set occlusion
        for obj in objects_local:
            if d_img is not None:
                obj.set_occlusion_by_depth(d_img, check_reference=check_reference)
            elif pc is not None:
                obj.set_occlusion_by_lidar(pc, check_reference=check_reference)
            else:
                print('Could not set occlusion!')

        # -- filter to only non-complete, known occlusions
        objects_local = [obj for obj in objects_local if obj.occlusion not in [Occlusion.COMPLETE, Occlusion.UNKNOWN]]

    # -- save objects to sensor files
    obj_file = CDM.npc_files['frame'][i_frame]
    CDM.save_objects(i_frame, objects_local, obj_sens_folder, obj_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('data_dir', type=str)
    parser.add_argument('--multi', action="store_true", help="Enable for multiprocessing")
    args = parser.parse_args()

    main(args)
