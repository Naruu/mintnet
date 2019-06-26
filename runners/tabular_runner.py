from models.tabular_flow import Net
from torch.nn.utils import clip_grad_norm_, clip_grad_value_
import shutil
import tensorboardX
import logging
from torchvision.datasets import CIFAR10, MNIST, ImageFolder
import torchvision.transforms as transforms
from torch.utils.data import DataLoader, Subset
import torch.nn.functional as F
import numpy as np
import torch.optim as optim
import os
from models.cnn_flow import DataParallelWithSampling
from torchvision.utils import save_image, make_grid
from datasets.imagenet import ImageNet
from datasets.bsds300 import BSDS300
from datasets.gas import GAS
from datasets.hepmass import HEPMASS
from datasets.miniboone import MINIBOONE
from datasets.power import POWER
import torch.autograd as autograd
import torch
import tqdm


class TabularRunner(object):
    def __init__(self, args, config):
        self.args = args
        self.config = config

    def get_optimizer(self, parameters):
        if self.config.optim.optimizer == 'Adam':
            return optim.Adam(parameters, lr=self.config.optim.lr, weight_decay=self.config.optim.weight_decay,
                              betas=(self.config.optim.beta1, 0.999), amsgrad=self.config.optim.amsgrad,
                              eps=self.config.optim.adam_eps)
        elif self.config.optim.optimizer == 'RMSProp':
            return optim.RMSprop(parameters, lr=self.config.optim.lr, weight_decay=self.config.optim.weight_decay)
        elif self.config.optim.optimizer == 'SGD':
            return optim.SGD(parameters, lr=self.config.optim.lr, momentum=0.9)
        elif self.config.optim.optimizer == 'Adamax':
            return optim.Adamax(parameters, lr=self.config.optim.lr, betas=(self.config.optim.beta1, 0.999),
                                weight_decay=self.config.optim.weight_decay)
        else:
            raise NotImplementedError('Optimizer {} not understood.'.format(self.config.optim.optimizer))


    def compute_grad_norm(self, model):
        # total_norm = 0.
        # for p in model.parameters():
        #     if p.requires_grad is True:
        #         total_norm += p.grad.data.norm().item() ** 2
        # return total_norm ** (1 / 2.)
        minv = np.inf
        maxv = -np.inf
        meanv = 0.
        total_p = 0
        for p in model.parameters():
            if p.requires_grad is True:
                minv = min(minv, p.grad.data.abs().min().item())
                maxv = max(maxv, p.grad.data.abs().max().item())
                meanv += p.grad.data.abs().sum().item()
                total_p += np.prod(p.grad.data.shape)
        return minv, maxv, meanv / total_p

    def batch_iter(self, X, batch_size, shuffle=False):
        """
        X: feature tensor (shape: num_instances x num_features)
        """
        if shuffle:
            idxs = torch.randperm(X.shape[0])
        else:
            idxs = torch.arange(X.shape[0])
        if X.is_cuda:
            idxs = idxs.cuda()
        for batch_idxs in idxs.split(batch_size):
            yield X[batch_idxs]

    def train(self):
        if self.config.data.dataset == 'bsds300':
            data = BSDS300('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = BSDS300('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'gas':
            data = GAS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = GAS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'hepmass':
            data = HEPMASS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = HEPMASS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'miniboone':
            data = MINIBOONE('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = MINIBOONE('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'power':
            data = POWER('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = POWER('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        dataloader = DataLoader(trn, batch_size=self.config.training.batch_size, shuffle=True, num_workers=4,
                                drop_last=True)
        test_loader = DataLoader(tst, batch_size=self.config.training.batch_size, shuffle=True,
                                 num_workers=4, drop_last=True)

        test_iter = iter(test_loader)

        net = Net(self.config).to(self.config.device)
        net = DataParallelWithSampling(net)

        optimizer = self.get_optimizer(net.parameters())

        tb_path = os.path.join(self.args.run, 'tensorboard', self.args.doc)
        if os.path.exists(tb_path):
            shutil.rmtree(tb_path)

        tb_logger = tensorboardX.SummaryWriter(log_dir=tb_path)

        def flow_loss(u, log_jacob, size_average=True):
            log_probs = (-0.5 * u.pow(2) - 0.5 * np.log(2 * np.pi)).sum()
            log_jacob = log_jacob.sum()
            loss = -(log_probs + log_jacob)

            if size_average:
                loss /= u.size(0)
            return loss

        # scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[50], gamma=0.1)
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, self.config.training.n_epochs, eta_min=0.)

        if self.args.resume_training:
            states = torch.load(os.path.join(self.args.run, 'logs', self.args.doc, 'checkpoint.pth'),
                                map_location=self.config.device)

            net.load_state_dict(states[0])
            optimizer.load_state_dict(states[1])
            begin_epoch = states[2]
            step = states[3]
            scheduler.load_state_dict(states[4])
        else:
            step = 0
            begin_epoch = 0

        # Train the model

        for epoch in range(begin_epoch, self.config.training.n_epochs):
            scheduler.step()
            for batch_idx, data in enumerate(dataloader):
                net.train()
                data = data.to(device=self.config.device)
                data = data.unsqueeze(dim=1)
                output, log_det = net(data)
                loss = flow_loss(output, log_det)

                # Backward and optimize
                optimizer.zero_grad()
                loss.backward()

                optimizer.step()
                bpd = (loss.item() * data.shape[0]) / (np.log(2) * np.prod(data.shape))

                # validation
                net.eval()
                with torch.no_grad():
                    try:
                        test_data = next(test_iter)
                    except StopIteration:
                        test_iter = iter(test_loader)
                        test_data = next(test_iter)

                    test_data = test_data.to(self.config.device)
                    test_data = test_data.unsqueeze(dim=1)

                    test_output, test_log_det = net(test_data)
                    test_loss = flow_loss(test_output, test_log_det)
                    test_bpd = (test_loss.item() * test_data.shape[0]) * (
                            1 / (np.log(2) * np.prod(test_data.shape)))

                tb_logger.add_scalar('training_loss', loss, global_step=step)
                tb_logger.add_scalar('training_bpd', bpd, global_step=step)
                tb_logger.add_scalar('test_loss', test_loss, global_step=step)
                tb_logger.add_scalar('test_bpd', test_bpd, global_step=step)

                if step % self.config.training.log_interval == 0:
                    logging.info(
                        "epoch: {}, batch: {}, training_loss: {}, test_loss: {}, traing_bpd: {}, test_bpd: {}".format(epoch, batch_idx, loss.item(),
                                                                                        test_loss.item(), bpd.item(), test_bpd.item()))
                step += 1

                # if self.config.data.dataset == 'bsds300':
                #     scheduler.step()
                #     if step % self.config.training.snapshot_interval == 0:
                #         states = [
                #             net.state_dict(),
                #             optimizer.state_dict(),
                #             epoch + 1,
                #             step,
                #             scheduler.state_dict()
                #         ]
                #         torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc,
                #                                         'checkpoint_batch_{}.pth'.format(step)))
                #         torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc, 'checkpoint.pth'))

                # if step == self.config.training.maximum_steps:
                #     states = [
                #         net.state_dict(),
                #         optimizer.state_dict(),
                #         epoch + 1,
                #         step,
                #         scheduler.state_dict()
                #     ]
                #     torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc,
                #                                     'checkpoint_last_batch.pth'))
                #     torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc, 'checkpoint.pth'))
                #
                #     return 0

            if (epoch + 1) % self.config.training.snapshot_interval == 0:
                states = [
                    net.state_dict(),
                    optimizer.state_dict(),
                    epoch + 1,
                    step,
                    scheduler.state_dict()
                ]
                torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc,
                                                'checkpoint_epoch_{}.pth'.format(epoch + 1)))
                torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc, 'checkpoint.pth'))

    def Langevin_dynamics(self, x_mod, net, n_steps=200, step_lr=0.00005):
        images = []

        def log_prob(x):
            u, log_jacob = net(x)
            log_probs = (-0.5 * u.pow(2) - 0.5 * np.log(2 * np.pi)).sum()
            log_jacob = log_jacob.sum()
            loss = (log_probs + log_jacob)
            return loss

        def score(x):
            with torch.enable_grad():
                x.requires_grad_(True)
                return autograd.grad(log_prob(x), x)[0]

        with torch.no_grad():
            for _ in range(n_steps):
                images.append(torch.clamp(x_mod, 0.0, 1.0))
                noise = torch.randn_like(x_mod) * np.sqrt(step_lr * 2)
                grad = score(x_mod)
                x_mod = x_mod + step_lr * grad + noise
                x_mod = x_mod
                print("modulus of grad components: mean {}, max {}".format(grad.abs().mean(), grad.abs().max()))

            return images

    def test(self):
        if self.config.data.dataset == 'bsds300':
            data = BSDS300('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = BSDS300('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'gas':
            data = GAS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = GAS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'hepmass':
            data = HEPMASS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = HEPMASS('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'miniboone':
            data = MINIBOONE('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = MINIBOONE('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x

        elif self.config.data.dataset == 'power':
            data = POWER('/atlas/u/yangsong/conv_flow/run/datasets/tabular/')
            trn = data.trn.x
            test_data = POWER('/atlas/u/yangsong/conv_flow/run/datasets/tabular/', train=False)
            tst = test_data.tst.x


        test_loader = DataLoader(tst, batch_size=self.config.training.batch_size, shuffle=True,
                                 num_workers=4, drop_last=True)

        net = Net(self.config).to(self.config.device)
        net = DataParallelWithSampling(net)
        optimizer = self.get_optimizer(net.parameters())

        def flow_loss(u, log_jacob, size_average=True):
            log_probs = (-0.5 * u.pow(2) - 0.5 * np.log(2 * np.pi)).sum()
            log_jacob = log_jacob.sum()
            loss = -(log_probs + log_jacob)

            if size_average:
                loss /= u.size(0)
            return loss

        states = torch.load(os.path.join(self.args.run, 'logs', self.args.doc, 'checkpoint.pth'),
                            map_location=self.config.device)

        net.load_state_dict(states[0])
        optimizer.load_state_dict(states[1])
        loaded_epoch = states[2]

        logging.info(
            "Loading the model from epoch {}".format(loaded_epoch))

        # Test the model
        net.eval()
        total_loss = 0
        total_bpd = 0
        total_n_data = 0

        logging.info("Calculating overall bpd")

        with torch.no_grad():
            for batch_idx, test_data in enumerate(test_loader):
                test_data = test_data.to(self.config.device)
                test_data = test_data.unsqueeze(dim=1)
                test_output, test_log_det = net(test_data)
                test_loss = flow_loss(test_output, test_log_det)

                test_bpd = (test_loss.item() * test_data.shape[0]) * (
                        1 / (np.log(2) * np.prod(test_data.shape)))

                total_loss += test_loss * test_data.shape[0]
                total_bpd += test_bpd * test_data.shape[0]
                total_n_data += test_data.shape[0]
        logging.info(
            "Total batch:{}\nTotal loss: {}\nTotal bpd: {}".format(batch_idx + 1, total_loss.item() / total_n_data,
                                                                   total_bpd.item() / total_n_data))