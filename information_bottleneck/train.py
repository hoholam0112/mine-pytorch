""" Training information bottleneck model """

import os
from collections import defaultdict

import numpy as np
import torch, torchvision
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Function
from torch.utils.tensorboard import SummaryWriter
import progressbar

from information_bottleneck.model import StatisticsNetwork, Classifier
from data import get_dataset
from mine import MINE
from torchutils.metrics import Accuracy, Mean
from torchutils.optim import build_optimizer

class GradientReversalLayer(Function):
    """ Negate gradient in backward pass """

    @staticmethod
    def forward(ctx, x, beta):
        ctx.beta = beta
        return x

    @staticmethod
    def backward(ctx, grad_output):
        return - ctx.beta * grad_output, None

def adaptive_clipping(loss_main,
                     loss_sub,
                     params,
                     optimizer):
    """ Let grad_sub be a gradient of params w.r.t. loss_sub and
        grad_main be gradient w.r.t. loss_main.
        Adaptive clipping forces grad_sub's norm not to be greater than grad_main's norm.
        In this function, loss_main.backward() and loss_sub.backward() are called.
        optimizer.step() is not called.

    Args:
        loss_main (torch.Tensor)
        loss_sub (torch.Tensor)
        params (python generator)
        optimizer (torch.optim.Optimizer)

    Returns:

    """
    # Backward pass for sub loss
    optimizer.zero_grad()
    loss_sub.backward(retain_graph=True)
    # Compute gradient norm
    grads_sub = []
    norm_sub = 0.0
    for param in params():
        if param.grad is None:
            grads_sub.append(0.0)
        else:
            grad_detached = param.grad.detach()
            grads_sub.append(grad_detached)
            norm_sub += torch.sum(grad_detached**2)
    norm_sub = torch.sqrt(norm_sub).item()

    # Backward pass for main loss
    optimizer.zero_grad()
    loss_main.backward()
    # Compute gradient norm
    norm_main = 0.0
    for param in params():
        grad_detached = param.grad.detach()
        norm_main += torch.sum(grad_detached**2)
    norm_main = torch.sqrt(norm_main)

    # Adaptive clipping
    for param, grad_sub in zip(params(), grads_sub):
        scale = min(norm_sub, norm_main) / norm_sub
        grad_sub *= scale
        param.grad += grad_sub

def run(args):
    """ train model """
    # Define argument 
    device = torch.device('cuda:{}'.format(args.gpu)
                    if torch.cuda.is_available() else 'cpu')

    dataset_name = args.dataset_name
    model_name = args.model_name
    tag = args.tag

    opt_name = args.opt_name or 'sgd'
    lr_clf = args.lr_clf or 0.05
    lr_mine = args.lr_mine or 1e-5
    weight_decay = args.weight_decay or 0.0

    batch_size = args.batch_size or 128
    total_epochs = args.epochs or 10000

    bottleneck_dim = args.bottleneck_dim or 256
    beta = args.beta or 1e-3
    ema_decay = 0.999

    # Load checkpoint file
    checkpoint_dir = './train_logs/{}/{}'.format(dataset_name, model_name)
    os.makedirs(checkpoint_dir, exist_ok=True)
    checkpoint_path = os.path.join(checkpoint_dir, '{}.tar'.format(tag))
    if os.path.exists(checkpoint_path):
        general_state_dict = torch.load(checkpoint_path)
        bottleneck_dim = general_state_dict['bottleneck_dim']
    else:
        general_state_dict = None

    # Build modules
    clf_name = dataset_name + '_' + model_name
    clf = Classifier(name=clf_name,
                     bottleneck_dim=bottleneck_dim)
    clf.to(device)

    statistics_network = StatisticsNetwork(name=dataset_name,
                                           bottleneck_dim=bottleneck_dim,
                                           noise='additive')
    statistics_network.to(device)

    # Build optimizer
    global_option = {'lr': lr_clf,
                     'weight_decay' : weight_decay}
    if opt_name in ['sgd', 'rmsprop']:
        global_option['momentum'] = 0.9

    per_param_options = [{'params' : clf.parameters()}]
    optimizer_clf = build_optimizer(opt_name,
                                    per_param_options,
                                    global_option)

    per_param_options = [{'params' : statistics_network.parameters(),
                          'lr': lr_mine}]
    optimizer_mine = build_optimizer(opt_name,
                                     per_param_options,
                                     global_option)

    # Define data loader
    dataset = get_dataset(dataset_name)
    loader = {}
    for k, dset in dataset.items():
        shuffle = (k == 'train')
        loader[k] = torch.utils.data.DataLoader(dset, batch_size=batch_size,
                shuffle=shuffle, pin_memory=True, num_workers=4)

    # Define loss function
    cross_entropy_loss = nn.CrossEntropyLoss()

    # Train a model
    clf.train()
    statistics_network.train()
    mine_object = MINE(statistics_network,
                       ema_decay)

    metric = {'train_error' : Accuracy(),
              'xent_loss' : Mean(),
              'eMI' : Mean(),
              'valid_error' : Accuracy()}

    if general_state_dict is not None:
        epoch = general_state_dict['epoch']
        best_val_error = general_state_dict['best_val_error']
        optimizer_clf.load_state_dict(general_state_dict['optimizer_clf'])
        clf.load_state_dict(general_state_dict['clf'])
        statistics_network.load_state_dict(
                general_state_dict['statistics_network'])
        optimizer_mine.load_state_dict(general_state_dict['optimizer_mine'])
    else:
        epoch = 0
        best_val_error = None

    #epochs_phase_one = 20
    #epochs_phase_two = 40

    steps_clf = 1
    steps_mine = 2

    while epoch < total_epochs:
        i = 1
        max_value = progressbar.UnknownLength
        with progressbar.ProgressBar(max_value) as pbar:
            # Train epoch
            metric['train_error'].reset_state()
            metric['xent_loss'].reset_state()
            metric['eMI'].reset_state()

            for x, y_true in loader['train']:
                x = x.to(device)
                y_true = y_true.to(device)

                # Forward pass
                y_pred, bottleneck = clf(x)
                loss = cross_entropy_loss(y_pred, y_true)
                metric['train_error'].update_state(y_pred, y_true)
                metric['xent_loss'].update_state(
                        loss.detach().cpu() * torch.ones(y_true.size(0), dtype=torch.float32))

                #if (model_name == 'mine') and (epoch > epochs_phase_one):
                #if (model_name == 'mine'):
                    #if epoch <= epochs_phase_two:
                    #    bottleneck = bottleneck.detach()

                #bottleneck = GradientReversalLayer.apply(bottleneck, beta)
                if model_name == 'base':
                    bottleneck = bottleneck.detach()
                eMI, loss_ib = mine_object.estimate_on_batch(x, bottleneck)
                metric['eMI'].update_state(
                        eMI.detach().cpu() * torch.ones(y_true.size(0), dtype=torch.float32))

                # Backward pass
                if model_name == 'mine':
                    if i%10 == 1:
                        #adaptive_clipping(loss, beta*eMI, clf.parameters, optimizer_clf)
                        optimizer_clf.zero_grad()
                        loss += loss_ib
                        loss.backward()
                        optimizer_clf.step()
                    else:
                        optimizer_mine.zero_grad()
                        loss_ib.backward()
                        optimizer_mine.step()
                elif model_name == 'base':
                    if epoch <= 100:
                        optimizer_clf.zero_grad()
                        loss.backward()
                        optimizer_clf.step()
                    else:
                        optimizer_mine.zero_grad()
                        loss_ib.backward()
                        optimizer_mine.step()

                pbar.update(i)
                i += 1

        # Validation
        metric['valid_error'].reset_state()
        for x, y_true in loader['valid']:
            x = x.to(device)
            y_true = y_true.to(device)
            with torch.no_grad():
                y_pred, _ = clf(x)
            metric['valid_error'].update_state(y_pred, y_true)

        epoch += 1
        print('tag: {}'.format(tag))
        print('Epoch: {:d}/{:d}'.format(epoch, total_epochs), end='')
        if best_val_error is not None:
            print(', best_valid_error: {:.2f}'.format(best_val_error))
        else:
            print('')

        for k, v in metric.items():
            if k in ['valid_error', 'train_error']:
                result = (1.0 - v.result()) * 100
                print('{}: {:.2f}'.format(k, result))
            else:
                print('{}: {:.4f}'.format(k, v.result()))

        # Save model
        curr_val_error = (1.0 - metric['valid_error'].result()) * 100
        if best_val_error is None:
            best_val_error = curr_val_error
        else:
            if best_val_error > curr_val_error:
                best_val_error = curr_val_error

                general_state_dict = {'optimizer_clf' : optimizer_clf.state_dict(),
                                      'optimizer_mine' : optimizer_mine.state_dict(),
                                      'clf' : clf.state_dict(),
                                      'statistics_network' : statistics_network.state_dict(),
                                      'epoch' : epoch,
                                      'best_val_error' : best_val_error,
                                      'bottleneck_dim' : bottleneck_dim}
                torch.save(general_state_dict, checkpoint_path)
                print('Model saved.')

