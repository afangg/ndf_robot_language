import os, os.path as osp
import random
import numpy as np
import torch
import argparse
import time
import sys
print(sys.path)
sys.path.append('/home/afo/repos/ndf_robot_language/src/')
import pybullet as p
import trimesh
import ndf_robot.model.vnn_occupancy_net_pointnet_dgcnn as vnn_occupancy_network
from ndf_robot.utils import util, trimesh_util
from ndf_robot.utils import path_util

from ndf_robot.utils.franka_ik import FrankaIK
from ndf_robot.opt.optimizer import OccNetOptimizer
from ndf_robot.robot.multicam import MultiCams
from ndf_robot.config.default_eval_cfg import get_eval_cfg_defaults
from ndf_robot.config.default_obj_cfg import get_obj_cfg_defaults
from ndf_robot.share.globals import bad_shapenet_mug_ids_list, bad_shapenet_bowls_ids_list, bad_shapenet_bottles_ids_list
from ndf_robot.utils.pipeline_util import (
    safeCollisionFilterPair,
    safeRemoveConstraint,
    soft_grasp_close,
    object_is_still_grasped,
    constraint_grasp_close,
    process_xq_data,
    process_xq_rs_data,
    process_demo_data,
    post_process_grasp_point,
    get_ee_offset,
    constraint_obj_world,
    constraint_grasp_open
)

from airobot import Robot, log_info, set_log_level, log_warn
from airobot.utils import common
from airobot.utils.common import euler2quat

from sentence_transformers import SentenceTransformer
from sentence_transformers import util as sentence_util

def show_link(obj_id, link_id, color):
    if link_id is not None:
        p.changeVisualShape(obj_id, link_id, rgbaColor=color)

class Pipeline():

    def __init__(self, global_dict, args):
        self.obj_classes = ['bottle', 'mug', 'bowl']
        self.ll_model = global_dict['ll_model']
        self.model = global_dict['ndf_model']
        self.all_objs_dirs = global_dict['all_objs_dirs']
        self.all_demos_dirs = global_dict['all_demos_dirs']

        self.random_pos = False
        self.ee_pose = None
        self.scene_obj = None
        self.table_model = None

        self.cfg = get_eval_cfg_defaults()

    
        self.load_robot(args)
        self.load_cams()
    
        self.demo_dic = self.get_demo_dict()
        print('All demo labels:', self.demo_dic.keys())

        random.seed(args.seed)
        np.random.seed(args.seed)
    
    def step(self, scene):
        while True:
            x = input('Press 1 to continue or 2 to use a new object\n')
            if x == '1':
                self.ee_pose = scene['final_ee_pos']
                # robot.arm.get_ee_pose()
                self.scene_obj = scene['obj_pcd'], scene['obj_pose'], scene['obj_id']
                break
            elif x == '2':
                self.robot.pb_client.remove_body(scene['obj_id'])
                # self.robot.arm.go_home(ignore_physics=True)
                # self.robot.arm.move_ee_xyz([0, 0, 0.2])
                # self.robot.arm.eetool.open()

                self.scene_obj = None
                self.ee_pose = None
                self.initial_poses = None
                self.table_model = None
                time.sleep(1.5)
                break

    def get_demo_dict(self):
        demo_dic = {}
        for demo_class in os.listdir(self.all_demos_dirs):
            class_path = osp.join(self.all_demos_dirs, demo_class)
            for demo_dir in os.listdir(class_path):
                demos_path = osp.join(class_path, demo_dir)
                concept = demo_class + ' '
                for fname in os.listdir(demos_path):
                    if '_demo_' not in fname: continue
                    verb = fname.split('_demo_')[0]
                    if concept+verb not in demo_dic:
                        demo_dic[concept+verb] = []
                    file_path = osp.join(demos_path, fname)
                    demo_dic[concept+verb].append(file_path)
        return demo_dic

    def get_env_cfgs(self):
        # general experiment + environment setup/scene generation configs
        cfg = get_eval_cfg_defaults()
        class_cfg = 'eval_'+self.test_obj+'_gen.yaml'
        config_fname = osp.join(path_util.get_ndf_config(), 'eval_cfgs', class_cfg)
        if osp.exists(config_fname):
            cfg.merge_from_file(config_fname)
            log_info('Config file loaded')
        else:
            log_info('Config file %s does not exist, using defaults' % config_fname)
        cfg.freeze()
        return cfg

    def get_obj_cfgs(self):
        # object specific configs
        obj_cfg = get_obj_cfg_defaults()
        obj_config_name = osp.join(path_util.get_ndf_config(), self.test_obj+'_obj_cfg.yaml')
        obj_cfg.merge_from_file(obj_config_name)
        obj_cfg.freeze()
        log_info("Set up config settings for %s" % self.test_obj)
        return obj_cfg

    def load_robot(self, args):
        self.robot = Robot('franka', pb_cfg={'gui': args.pybullet_viz}, arm_cfg={'self_collision': False, 'seed': args.seed})
        self.ik_helper = FrankaIK(gui=False)
        
        self.finger_joint_id = 9
        self.left_pad_id = 9
        self.right_pad_id = 10

        p.changeDynamics(self.robot.arm.robot_id, self.left_pad_id, lateralFriction=1.0)
        p.changeDynamics(self.robot.arm.robot_id, self.right_pad_id, lateralFriction=1.0)

        # reset
        self.robot.arm.reset(force_reset=True)
        self.robot.cam.setup_camera(
            focus_pt=[0.4, 0.0, self.cfg.TABLE_Z],
            dist=0.9,
            yaw=45,
            pitch=-25,
            roll=0)

    def load_cams(self):
        self.cams = MultiCams(self.cfg.CAMERA, self.robot.pb_client, n_cams=self.cfg.N_CAMERAS)
        log_info('Number of cameras: %s' % len(self.cams.cams))
        cam_info = {}
        cam_info['pose_world'] = []
        for cam in self.cams.cams:
            cam_info['pose_world'].append(util.pose_from_matrix(cam.cam_ext_mat))

    def load_table(self):            
        self.cfg = self.get_env_cfgs()
        self.obj_cfgs = self.get_obj_cfgs()

        # put table at right spot
        table_ori = euler2quat([0, 0, np.pi / 2])

        # this is the URDF that was used in the demos -- make sure we load an identical one
        tmp_urdf_fname = osp.join(path_util.get_ndf_descriptions(), 'hanging/table/table_tmp.urdf')
        open(tmp_urdf_fname, 'w').write(self.table_model)
        self.table_id = self.robot.pb_client.load_urdf(tmp_urdf_fname,
                                self.cfg.TABLE_POS,
                                table_ori,
                                scaling=self.cfg.TABLE_SCALING)

        if self.cfg.DEMOS.PLACEMENT_SURFACE == 'shelf':
            self.load_shelf = True
            log_info('Shelf loaded')
        else:
            log_info('Rack loaded')
            self.load_shelf = False
        
        self.robot.arm.go_home(ignore_physics=True)
        self.robot.arm.move_ee_xyz([0, 0, 0.2])
        self.robot.arm.eetool.open()
        time.sleep(1.5)
        log_info("DONE SETTING UP")

    def prompt_query(self):
        self.demos = self.choose_demos()
        if not len(self.demos):
            log_warn('No demos correspond to the query!')

    def choose_demos(self):
        concepts = list(self.demo_dic.keys())
        n = len(concepts)
        concept_embeddings = self.ll_model.encode(concepts, convert_to_tensor=True)

        while True:
            query_text = input('Please enter a query\n')
            target_embedding= self.ll_model.encode(query_text, convert_to_tensor=True)
            scores = sentence_util.pytorch_cos_sim(target_embedding, concept_embeddings)
            sorted_scores, idx = torch.sort(scores, descending=True)
            sorted_scores, idx = sorted_scores.flatten(), idx.flatten()
            corresponding_concept = None
            for i in range(n):
                print('Best matches:', concepts[idx[i]])
                query_text = input('Corrent? (y/n)\n')
                if query_text == 'n':
                    continue
                elif query_text == 'y':
                    corresponding_concept = concepts[idx[i]]
                    break
            
            if corresponding_concept:
                break

        demos = self.demo_dic[corresponding_concept] if corresponding_concept in self.demo_dic else []
        if not len(demos):
            log_warn('No demos correspond to the query!')
            
        log_info('Number of Demos %s' % len(demos))
        
        self.test_obj = [obj for obj in self.obj_classes if obj in corresponding_concept][0]

        if demos is not None and self.table_model is None:
            log_info('Loading new table')
            self.table_model = np.load(demos[0], allow_pickle=True)['table_urdf'].item()
        return demos

    def get_test_objs(self):
        #either replace with random objects (manipulation can fail) or label object in demo
        test_obj = self.test_obj
        avoid_shapenet_ids = []
        if test_obj == 'mug':
            avoid_shapenet_ids = bad_shapenet_mug_ids_list + self.cfg.MUG.AVOID_SHAPENET_IDS
        elif test_obj == 'bowl':
            avoid_shapenet_ids = bad_shapenet_bowls_ids_list + self.cfg.BOWL.AVOID_SHAPENET_IDS
        elif test_obj == 'bottle':
            avoid_shapenet_ids = bad_shapenet_bottles_ids_list + self.cfg.BOTTLE.AVOID_SHAPENET_IDS 
        else:
            test_shapenet_ids = []
        # get objects that we can use for testing
        test_object_ids = {}
        shapenet_obj_dir = osp.join(path_util.get_ndf_obj_descriptions(), test_obj + '_centered_obj_normalized')
        shapenet_id_list = [fn.split('_')[0] for fn in os.listdir(shapenet_obj_dir)] if test_obj == 'mug' else os.listdir(shapenet_obj_dir)
        for s_id in shapenet_id_list:
            valid = s_id not in avoid_shapenet_ids
            if valid:
                # for testing, use the "normalized" object
                obj_obj_file = osp.join(shapenet_obj_dir, s_id, 'models/model_normalized.obj')
                test_object_ids[s_id] = obj_obj_file
        self.test_obj = test_obj
        return test_object_ids

    def add_object(self, test_obj_ids):
        x_low, x_high = self.cfg.OBJ_SAMPLE_X_HIGH_LOW
        y_low, y_high = self.cfg.OBJ_SAMPLE_Y_HIGH_LOW
        table_z = self.cfg.TABLE_Z

        if self.test_obj == 'mug':
            rack_link_id = 0
            shelf_link_id = 1
        elif self.test_obj in ['bowl', 'bottle']:
            rack_link_id = None
            shelf_link_id = 0

        if self.cfg.DEMOS.PLACEMENT_SURFACE == 'shelf':
            self.placement_link_id = shelf_link_id
        else:
            self.placement_link_id = rack_link_id 

        scale_default = self.cfg.MESH_SCALE_DEFAULT
        mesh_scale=[scale_default] * 3
        if self.random_pos:
            if self.test_obj in ['bowl', 'bottle']:
                rp = np.random.rand(2) * (2 * np.pi / 3) - (np.pi / 3)
                ori = common.euler2quat([rp[0], rp[1], 0]).tolist()
            else:
                rpy = np.random.rand(3) * (2 * np.pi / 3) - (np.pi / 3)
                ori = common.euler2quat([rpy[0], rpy[1], rpy[2]]).tolist()
            pos = [
                np.random.random() * (x_high - x_low) + x_low,
                np.random.random() * (y_high - y_low) + y_low,
                self.cfg.TABLE_Z]
            pose = pos + ori
            rand_yaw_T = util.rand_body_yaw_transform(pos, min_theta=-np.pi, max_theta=np.pi)
            pose_w_yaw = util.transform_pose(util.list2pose_stamped(pose), util.pose_from_matrix(rand_yaw_T))
            pos, ori = util.pose_stamped2list(pose_w_yaw)[:3], util.pose_stamped2list(pose_w_yaw)[3:]

        else:
            upright_orientation = common.euler2quat([np.pi/2, 0, 0]).tolist()
            pos = [np.random.random() * (x_high - x_low) + x_low, np.random.random() * (y_high - y_low) + y_low, table_z]
            pose = util.list2pose_stamped(pos + upright_orientation)
            rand_yaw_T = util.rand_body_yaw_transform(pos, min_theta=-np.pi, max_theta=np.pi)
            pose_w_yaw = util.transform_pose(pose, util.pose_from_matrix(rand_yaw_T))
            pos, ori = util.pose_stamped2list(pose_w_yaw)[:3], util.pose_stamped2list(pose_w_yaw)[3:]


        obj_shapenet_id = random.sample(test_obj_ids.keys(), 1)[0]
        id_str = 'Loading Shapenet ID: %s' % obj_shapenet_id
        print(id_str)

        # for testing, use the "normalized" object
        obj_file = test_obj_ids[obj_shapenet_id]
        obj_file_dec = obj_file.split('.obj')[0] + '_dec.obj'
        # convert mesh with vhacd
        if not osp.exists(obj_file_dec):
            p.vhacd(
                obj_file,
                obj_file_dec,
                'log.txt',
                concavity=0.0025,
                alpha=0.04,
                beta=0.05,
                gamma=0.00125,
                minVolumePerCH=0.0001,
                resolution=1000000,
                depth=20,
                planeDownsampling=4,
                convexhullDownsampling=4,
                pca=0,
                mode=0,
                convexhullApproximation=1
            )

        obj_id = self.robot.pb_client.load_geom(
            'mesh',
            mass=0.01,
            mesh_scale=mesh_scale,
            visualfile=obj_file_dec,
            collifile=obj_file_dec,
            base_pos=pos,
            base_ori=ori)
        p.changeDynamics(obj_id, -1, lateralFriction=0.5)

        safeCollisionFilterPair(obj_id, self.table_id, -1, -1, enableCollision=True)
        safeCollisionFilterPair(self.robot.arm.robot_id, self.table_id, -1, -1, enableCollision=True)

        p.changeDynamics(obj_id, -1, linearDamping=5, angularDamping=5)

        if self.test_obj == 'mug':
            rack_color = p.getVisualShapeData(self.table_id)[rack_link_id][7]
            show_link(self.table_id, rack_link_id, rack_color)
        return obj_id, pos, ori

    def segment_pcd(self, obj_id):
        depth_imgs = []
        seg_idxs = []
        obj_pcd_pts = []
        table_pcd_pts = []

        obj_pose_world = p.getBasePositionAndOrientation(obj_id)
        obj_pose_world = util.list2pose_stamped(list(obj_pose_world[0]) + list(obj_pose_world[1]))
        for i, cam in enumerate(self.cams.cams): 
            # get image and raw point cloud
            rgb, depth, seg = cam.get_images(get_rgb=True, get_depth=True, get_seg=True)
            pts_raw, _ = cam.get_pcd(in_world=True, rgb_image=rgb, depth_image=depth, depth_min=0.0, depth_max=np.inf)

            # flatten and find corresponding pixels in segmentation mask
            flat_seg = seg.flatten()
            flat_depth = depth.flatten()
            obj_inds = np.where(flat_seg == obj_id)
            table_inds = np.where(flat_seg == self.table_id)
            seg_depth = flat_depth[obj_inds[0]]  
            
            obj_pts = pts_raw[obj_inds[0], :]
            obj_pcd_pts.append(util.crop_pcd(obj_pts))

            table_pts = pts_raw[table_inds[0], :][::int(table_inds[0].shape[0]/500)]
            table_pcd_pts.append(table_pts)

            depth_imgs.append(seg_depth)
            seg_idxs.append(obj_inds)

        target_obj_pcd_obs = np.concatenate(obj_pcd_pts, axis=0)  # object shape point cloud
        target_pts_mean = np.mean(target_obj_pcd_obs, axis=0)
        inliers = np.where(np.linalg.norm(target_obj_pcd_obs - target_pts_mean, 2, 1) < 0.2)[0]
        target_obj_pcd_obs = target_obj_pcd_obs[inliers]
        return target_obj_pcd_obs, obj_pose_world

    def load_optimizer(self, demos, n=None):
        if n is None:
            n = len(demos)
        else:
            demos[:n]
        demo_target_info = []
        demo_shapenet_ids = []
        initial_poses = {}
        query_pts, query_pts_rs = None, None
        for fname in demos:
            target_info = None
            data = np.load(fname, allow_pickle=True)
            if query_pts is None:
                query_pts  = process_xq_data(data, shelf=self.load_shelf)
            if query_pts_rs is None:
                query_pts_rs = process_xq_rs_data(data, shelf=self.load_shelf)               
            demo_shapenet_id = data['shapenet_id'].item()

            if self.scene_obj is None:
                target_info, initial_pose = process_demo_data(data, shelf=self.load_shelf)
                initial_poses[demo_shapenet_id] = initial_pose
            elif self.scene_obj is not None and demo_shapenet_id in self.initial_poses:
                target_info, _ = process_demo_data(data, self.initial_poses[demo_shapenet_id], shelf=self.load_shelf)
                del self.initial_poses[demo_shapenet_id]
            else:
                continue

            if target_info is not None:
                demo_target_info.append(target_info)
                demo_shapenet_ids.append(demo_shapenet_id)
            else:
                log_info('Could not load demo')


        self.initial_poses = initial_poses
        optimizer = OccNetOptimizer(self.model, query_pts=query_pts, query_pts_real_shape=query_pts_rs, opt_iterations=500)
        optimizer.set_demo_info(demo_target_info)
        log_info("OPTIMIZER LOADED")
        return optimizer

    def find_correspondence(self, optimizer, args, target_obj_pcd, obj_pose_world):
        ee_poses = []
        if self.scene_obj is None:
            #grasping
            log_info('Solve for pre-grasp coorespondance')
            pre_ee_pose_mats, best_idx = optimizer.optimize_transform_implicit(target_obj_pcd, ee=True)
            pre_ee_pose = util.pose_stamped2list(util.pose_from_matrix(pre_ee_pose_mats[best_idx]))
            # grasping requires post processing to find anti-podal point
            grasp_pt = post_process_grasp_point(pre_ee_pose, target_obj_pcd, thin_feature=(not args.non_thin_feature), grasp_viz=args.grasp_viz, grasp_dist_thresh=args.grasp_dist_thresh)
            pre_ee_pose[:3] = grasp_pt
            pre_ee_offset_tf = get_ee_offset(ee_pose=pre_ee_pose)
            pre_pre_ee_pose = util.pose_stamped2list(
                util.transform_pose(pose_source=util.list2pose_stamped(pre_ee_pose), pose_transform=util.list2pose_stamped(pre_ee_offset_tf)))

            ee_poses.append(pre_pre_ee_pose)
            ee_poses.append(pre_ee_pose)
        else:
            #placement
            log_info('Solve for placement coorespondance')
            pose_mats, best_idx = optimizer.optimize_transform_implicit(target_obj_pcd, ee=False)
            relative_pose = util.pose_stamped2list(util.pose_from_matrix(pose_mats[best_idx]))
            ee_end_pose = util.transform_pose(pose_source=util.list2pose_stamped(self.ee_pose), pose_transform=util.list2pose_stamped(relative_pose))
            # obj_start_pose = obj_pose_world
            # obj_end_pose = util.transform_pose(pose_source=obj_start_pose, pose_transform=util.list2pose_stamped(rack_relative_pose))
            # obj_end_pose_list = util.pose_stamped2list(obj_end_pose)
            # ee_end_pose = util.transform_pose(pose_source=obj_start_pose, pose_transform=util.list2pose_stamped(relative_pose))
            # final_pcd = util.transform_pcd(target_obj_pcd, pose_mats[best_idx])
            # trimesh_util.trimesh_show([target_obj_pcd, final_pcd], show=True)
            preplace_offset_tf = util.list2pose_stamped(self.cfg.PREPLACE_OFFSET_TF)
            preplace_horizontal_tf = util.list2pose_stamped(self.cfg.PREPLACE_HORIZONTAL_OFFSET_TF)

            pre_ee_end_pose2 = util.transform_pose(pose_source=ee_end_pose, pose_transform=preplace_offset_tf)
            pre_ee_end_pose1 = util.transform_pose(pose_source=pre_ee_end_pose2, pose_transform=preplace_horizontal_tf)        
    
                # get pose that's straight up
            offset_pose = util.transform_pose(
                pose_source=util.list2pose_stamped(np.concatenate(self.robot.arm.get_ee_pose()[:2]).tolist()),
                pose_transform=util.list2pose_stamped([0, 0, 0.15, 0, 0, 0, 1])
            )
            ee_poses.append(util.pose_stamped2list(offset_pose)) 
            ee_poses.append(util.pose_stamped2list(pre_ee_end_pose1))
            ee_poses.append(util.pose_stamped2list(pre_ee_end_pose2))
            ee_poses.append(util.pose_stamped2list(ee_end_pose))

        log_info('Found correspondence')
        return ee_poses

    def pre_execution(self, obj_id, pos, ori, final_pos=None):
        if self.scene_obj is None:
            # reset everything
            self.robot.pb_client.set_step_sim(False)
            time.sleep(0.5)

            # turn OFF collisions between robot and object / table, and move to pre-grasp pose
            for i in range(p.getNumJoints(self.robot.arm.robot_id)):
                safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=self.table_id, linkIndexA=i, linkIndexB=-1, enableCollision=False, physicsClientId=self.robot.pb_client.get_client_id())
                safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=obj_id, linkIndexA=i, linkIndexB=-1, enableCollision=False, physicsClientId=self.robot.pb_client.get_client_id())
            self.robot.arm.eetool.open()
        else:
            # # reset object to placement pose to detect placement success
            # safeCollisionFilterPair(obj_id, self.table_id, -1, -1, enableCollision=False)
            # safeCollisionFilterPair(obj_id, self.table_id, -1, self.placement_link_id, enableCollision=False)
            # self.robot.pb_client.set_step_sim(True)
            # self.robot.pb_client.reset_body(obj_id, final_pos[:3], final_pos[3:])

            # time.sleep(0.2)
            # safeCollisionFilterPair(obj_id, self.table_id, -1, self.placement_link_id, enableCollision=True)
            # self.robot.pb_client.set_step_sim(False)
            # time.sleep(0.2)

            # safeCollisionFilterPair(obj_id, self.table_id, -1, -1, enableCollision=True)
            # self.robot.pb_client.reset_body(obj_id, pos, ori)
            pass

    def get_iks(self, ee_poses):
        # jnt_poses = [None]*len(ee_poses)
        # for i, pose in enumerate(ee_poses):
        #     log_info('Attempt to find IK %i' % i)
        #     jnt_poses[i] = self.cascade_ik(pose)
        return [self.cascade_ik(pose) for pose in ee_poses]
    
    def cascade_ik(self, ee_pose):
        jnt_pos = None
        if jnt_pos is None:
            jnt_pos = self.ik_helper.get_feasible_ik(ee_pose, verbose=True)
            if jnt_pos is None:
                jnt_pos = self.ik_helper.get_ik(ee_pose)
                if jnt_pos is None:
                    jnt_pos = self.robot.arm.compute_ik(ee_pose[:3], ee_pose[3:])
        if jnt_pos is None:
            log_warn('Failed to find IK')
        return jnt_pos

    # def plan_and_execute(self, poses, obj_id):
    #     prev_pos = self.robot.arm.get_jpos()
    #     for i, jnt_pos in enumerate(poses):
    #         if jnt_pos is None:
    #             log_warn('No IK for jnt', i)
    #             break
            
    #         plan = self.plan_motion(prev_pos, jnt_pos)
    #         self.execute_plan(plan)
    #         prev_pos = jnt_pos

    #         if i == 0:
    #             # turn ON collisions between robot and object, and close fingers
    #             for _ in range(p.getNumJoints(self.robot.arm.robot_id)):
    #                 safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=obj_id, linkIndexA=i, linkIndexB=-1, enableCollision=True, physicsClientId=self.robot.pb_client.get_client_id())
    #                 safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=self.table_id, linkIndexA=i, linkIndexB=self.placement_link_id, enableCollision=False, physicsClientId=self.robot.pb_client.get_client_id())
            
    #         input('Press enter to continue')

    def plan_motion(self, start_pos, goal_pos):
        return self.ik_helper.plan_joint_motion(start_pos, goal_pos)

    def execute_plan(self, plan):
        for jnt in plan:
            self.robot.arm.set_jpos(jnt, wait=False)
            time.sleep(0.025)
        self.robot.arm.set_jpos(plan[-1], wait=True)

    def allow_pregrasp_collision(self, obj_id):
        log_info('Turning off collision between gripper and object for pre-grasp')
        # turn ON collisions between robot and object, and close fingers
        for i in range(p.getNumJoints(self.robot.arm.robot_id)):
            safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=obj_id, linkIndexA=i, linkIndexB=-1, enableCollision=True, physicsClientId=self.robot.pb_client.get_client_id())
            safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=self.table_id, linkIndexA=i, linkIndexB=self.placement_link_id, enableCollision=False, physicsClientId=self.robot.pb_client.get_client_id())
    
    def is_pregrasp_state(self):
        return self.scene_obj is None

    def post_execution(self, obj_id, pos, ori):
        if self.scene_obj is None:
            # turn ON collisions between robot and object, and close fingers
            for i in range(p.getNumJoints(self.robot.arm.robot_id)):
                safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=obj_id, linkIndexA=i, linkIndexB=-1, enableCollision=True, physicsClientId=self.robot.pb_client.get_client_id())
                safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=self.table_id, linkIndexA=i, linkIndexB=self.placement_link_id, enableCollision=False, physicsClientId=self.robot.pb_client.get_client_id())

            time.sleep(0.8)
            jnt_pos_before_grasp = self.robot.arm.get_jpos()
            soft_grasp_close(self.robot, self.finger_joint_id, force=40)
            time.sleep(1.5)
            grasp_success = object_is_still_grasped(self.robot, obj_id, self.right_pad_id, self.left_pad_id) 
            time.sleep(1.5)

            if grasp_success:
                log_info("It is grasped")
                self.robot.arm.set_jpos(jnt_pos_before_grasp, ignore_physics=True)
                # self.o_cid = constraint_grasp_close(self.robot, obj_id)
            else:
                log_info('Not grasped')
        else:
            grasp_success = object_is_still_grasped(self.robot, obj_id, self.right_pad_id, self.left_pad_id) 
            if grasp_success:
                # turn ON collisions between object and rack, and open fingers
                safeCollisionFilterPair(obj_id, self.table_id, -1, -1, enableCollision=True)
                safeCollisionFilterPair(obj_id, self.table_id, -1, self.placement_link_id, enableCollision=True)
                
                p.changeDynamics(obj_id, -1, linearDamping=5, angularDamping=5)
                # constraint_grasp_open(self.o_cid)
                self.robot.arm.eetool.open()

                time.sleep(0.2)
                for i in range(p.getNumJoints(self.robot.arm.robot_id)):
                    safeCollisionFilterPair(bodyUniqueIdA=self.robot.arm.robot_id, bodyUniqueIdB=obj_id, linkIndexA=i, linkIndexB=-1, enableCollision=False, physicsClientId=self.robot.pb_client.get_client_id())
                self.robot.arm.move_ee_xyz([0, 0.075, 0.075])
                safeCollisionFilterPair(obj_id, self.table_id, -1, -1, enableCollision=False)
                time.sleep(4.0)
            else:
                log_warn('No object in hand')
