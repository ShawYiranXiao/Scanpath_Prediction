"""Test script.
Usage:
  test.py <hparams> <checkpoint_dir> <dataset_root> [--cuda=<id>]
  test.py -h | --help

Options:
  -h --help     Show this screen.
  --cuda=<id>   id of the cuda device [default: 0].
"""

import os
import json
import torch
import numpy as np
from torch.utils.data.dataset import Dataset
from tqdm import tqdm
from docopt import docopt
from os.path import join

from dataset import process_data
from irl_dcb.config import JsonConfig

import cv2 as cv

from irl_dcb.data import LHF_IRL
from irl_dcb.models import LHF_Policy_Cond_Small
from irl_dcb.environment import IRL_Env4LHF
from irl_dcb import utils

torch.manual_seed(42620)
np.random.seed(42620)

def gen_scanpaths(generator,
                  env_test,
                  test_img_loader,
                  patch_num,
                  max_traj_len,
                  im_w,
                  im_h,
                  num_sample=10):
    all_actions = []
    for i_sample in range(num_sample):
        progress = tqdm(test_img_loader,
                        desc='trial ({}/{})'.format(i_sample + 1, num_sample))
        # breakpoint()
        for i_batch, batch in enumerate(progress):
            # if "car" in batch['cat_name']:
                # breakpoint()
            env_test.set_data(batch)
            img_names_batch = batch['img_name']
            cat_names_batch = batch['cat_name']
            with torch.no_grad():
                env_test.reset()
                trajs = utils.collect_trajs(env_test,
                                            generator,
                                            patch_num,
                                            max_traj_len,
                                            is_eval=True,
                                            sample_action=True)
                all_actions.extend([(cat_names_batch[i], img_names_batch[i],
                                     'present', trajs['actions'][:, i])
                                    for i in range(env_test.batch_size)])

    scanpaths = utils.actions2scanpaths(all_actions, patch_num, im_w, im_h)
    utils.cutFixOnTarget(scanpaths, bbox_annos)

    return scanpaths


if __name__ == '__main__':
    args = docopt(__doc__)
    device = torch.device('cpu')
    hparams = args["<hparams>"]
    dataset_root = args["<dataset_root>"]
    checkpoint = args["<checkpoint_dir>"]
    hparams = JsonConfig(hparams)

    # dir of pre-computed beliefs
    #./
    DCB_dir_HR = join(dataset_root, 'DCBs/HR/') #./DCBs/HR/
    DCB_dir_LR = join(dataset_root, 'DCBs/LR/')
    data_name = '{}x{}'.format(hparams.Data.im_w, hparams.Data.im_h)
    # print(data_name)

    # bounding box of the target object (for search efficiency evaluation)
    bbox_annos = np.load(join(dataset_root, 'bbox_annos.npy'),
                         allow_pickle=True).item()
    # breakpoint()
    # bbox_annos["car_0001.jpg"] = [39, 178, 96, 79]
    with open(join(dataset_root,
                   'human_scanpaths_TP_trainval_train.json')) as json_file:
        human_scanpaths_train = json.load(json_file)

    with open(join(dataset_root,
                   'human_scanpaths_TP_trainval_valid.json')) as json_file:
        human_scanpaths_valid = json.load(json_file)

    target_init_fixs = {}
    for traj in human_scanpaths_train + human_scanpaths_valid:
        # if traj['task'] == "car" and traj['name'] ==  "0001.jpg":
        #     breakpoint()
        key = traj['task'] + '_' + traj['name']
        target_init_fixs[key] = (traj['X'][0] / hparams.Data.im_w,
                                 traj['Y'][0] / hparams.Data.im_h)
    # breakpoint()
    cat_names = list(np.unique([x['task'] for x in human_scanpaths_train]))
    catIds = dict(zip(cat_names, list(range(len(cat_names)))))

    dataset = process_data(human_scanpaths_train, human_scanpaths_valid,
                           DCB_dir_HR, DCB_dir_LR, bbox_annos, hparams)
    # breakpoint()
    train_task_img_pair = np.unique(
        # [traj['task'] + '_' + traj['name'] for traj in human_scanpaths_train + human_scanpaths_valid])
        [traj['task'] + '_' + traj['name'] for traj in human_scanpaths_valid])
        
    test_dataset = LHF_IRL(DCB_dir_HR, DCB_dir_LR, target_init_fixs, train_task_img_pair, bbox_annos, hparams.Data, catIds)
    # breakpoint()

    dataloader = torch.utils.data.DataLoader(test_dataset,
                                             batch_size=128,
                                             shuffle=False,
                                             num_workers=2)
    # load trained model
    # len(dataset['catIds']
    # task_eye

    input_size = 134  # number of belief maps
    task_eye = torch.eye(len(dataset['catIds'])).to(device)
    generator = LHF_Policy_Cond_Small(hparams.Data.patch_count,
                                      len(dataset['catIds']), task_eye,
                                      input_size).to(device)

    generator.eval()

    state = torch.load(join(checkpoint, 'trained_generator.pkg'), map_location=device)

    generator.load_state_dict(state["model"])

    # build environment
    env_test = IRL_Env4LHF(hparams.Data,
                           max_step=hparams.Data.max_traj_length,
                           mask_size=hparams.Data.IOR_size,
                           status_update_mtd=hparams.Train.stop_criteria,
                           device=device,
                           inhibit_return=True)

    # generate scanpaths
    # print('sample scanpaths (10 for each testing image)...')
    # predictions = gen_scanpaths(generator,
    #                             env_test,
    #                             dataloader,
    #                             hparams.Data.patch_num,
    #                             hparams.Data.max_traj_length,
    #                             hparams.Data.im_w,
    #                             hparams.Data.im_h,
    #                             num_sample=1)
    # Generate scanpaths and save to JSON
    predictions = gen_scanpaths(generator, env_test, dataloader, hparams.Data.patch_num, hparams.Data.max_traj_length, hparams.Data.im_w, hparams.Data.im_h, num_sample=1)

    scanpath_dict = []
    for elem in predictions:
        scanpoints = [{'X': elem['X'][i], 'Y': elem['Y'][i]} for i in range(len(elem['X']))]
        scanpath_dict.append({'task': elem['task'], 'name': elem['name'], 'scanpoints': scanpoints})

    with open('./results/scanpaths.json', 'w') as jsonfile:
        json.dump(scanpath_dict, jsonfile, indent=4)




    # print(predictions)
    # breakpoint()
    for elem in predictions:
        filename = elem['task']+"/" + elem['name']
        image = cv.imread("../images/"+filename)

        X = elem['X']
        Y = elem['Y']

        image = cv.resize(image, (hparams.Data.im_w, hparams.Data.im_h))

        for i in range(len(X)):
            x = int(X[i])
            y = int(Y[i])
            cv.putText(image, str(i), (x, y), cv.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255))
            cv.circle(image, (x, y), 2, (255, 255, 255))
            if i > 0:
                xprec = int(X[i-1])
                yprec = int(Y[i-1])
                cv.line(image, (xprec, yprec), (x, y), (255, 255, 255))

        os.makedirs("./results/" + elem['task'] + "/", exist_ok=True)
        cv.imwrite("./results/" + elem['task'] + "/" + elem['name'], image)
