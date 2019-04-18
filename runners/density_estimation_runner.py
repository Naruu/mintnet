from models.cnn_flow import *
# from models.cnn_new1 import *
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


class DensityEstimationRunner(object):
    def __init__(self, args, config):
        self.args = args
        self.config = config

    def get_optimizer(self, parameters):
        if self.config.optim.optimizer == 'Adam':
            return optim.Adam(parameters, lr=self.config.optim.lr, weight_decay=self.config.optim.weight_decay,
                              betas=(self.config.optim.beta1, 0.999))
        elif self.config.optim.optimizer == 'RMSProp':
            return optim.RMSprop(parameters, lr=self.config.optim.lr, weight_decay=self.config.optim.weight_decay)
        elif self.config.optim.optimizer == 'SGD':
            return optim.SGD(parameters, lr=self.config.optim.lr, momentum=0.9)
        elif self.config.optim.optimizer == 'Adamax':
            return optim.Adamax(parameters, lr=self.config.optim.lr, betas=(self.config.optim.beta1, 0.999),
                                weight_decay=self.config.optim.weight_decay)
        else:
            raise NotImplementedError('Optimizer {} not understood.'.format(self.config.optim.optimizer))

    def logit_transform(self, image):
        lambd = self.config.data.lambda_logit
        image = lambd + (1 - 2 * lambd) * image
        return torch.log(image) - torch.log1p(-image)

    def adjust_learning_rate(self, optimizer, epoch):
        """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
        if epoch - 530 == 0:# or epoch % 60 == 0:
            for param_group in optimizer.param_groups:
                param_group['lr'] *= 10

    def train(self):
        transform = transforms.Compose([
            transforms.Resize(self.config.data.image_size),
            transforms.ToTensor()
        ])

        if self.config.data.dataset == 'CIFAR10':
            dataset = CIFAR10(os.path.join(self.args.run, 'datasets', 'cifar10'), train=True, download=True,
                              transform=transform)
            test_dataset = CIFAR10(os.path.join(self.args.run, 'datasets', 'cifar10'), train=False, download=True,
                                   transform=transform)
        elif self.config.data.dataset == 'MNIST':
            dataset = MNIST(os.path.join(self.args.run, 'datasets', 'mnist'), train=True, download=True,
                            transform=transform)
            num_items = len(dataset)
            indices = list(range(num_items))
            random_state = np.random.get_state()
            np.random.seed(2019)
            np.random.shuffle(indices)
            np.random.set_state(random_state)
            train_indices, test_indices = indices[:int(num_items * 0.8)], indices[int(num_items * 0.8):]
            test_dataset = Subset(dataset, test_indices)
            dataset = Subset(dataset, train_indices)

        elif self.config.data.dataset == 'CELEBA':
            dataset = ImageFolder(root=os.path.join(self.args.run, 'datasets', 'celeba'),
                                  transform=transforms.Compose([
                                      transforms.CenterCrop(140),
                                      transforms.Resize(self.config.data.image_size),
                                      transforms.ToTensor(),
                                      transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                                  ]))
            num_items = len(dataset)
            indices = list(range(num_items))
            random_state = np.random.get_state()
            np.random.seed(2019)
            np.random.shuffle(indices)
            np.random.set_state(random_state)
            train_indices, test_indices = indices[:int(num_items * 0.7)], indices[
                                                                          int(num_items * 0.7):int(num_items * 0.8)]
            test_dataset = Subset(dataset, test_indices)
            dataset = Subset(dataset, train_indices)

        dataloader = DataLoader(dataset, batch_size=self.config.training.batch_size, shuffle=True, num_workers=4,
                                drop_last=True)
        test_loader = DataLoader(test_dataset, batch_size=self.config.training.batch_size, shuffle=True,
                                 num_workers=4, drop_last=True)
        test_iter = iter(test_loader)

        net = Net(self.config).to(self.config.device)
        net = torch.nn.DataParallel(net)

        optimizer = self.get_optimizer(net.parameters())

        tb_path = os.path.join(self.args.run, 'tensorboard', self.args.doc)
        if os.path.exists(tb_path):
            shutil.rmtree(tb_path)

        tb_logger = tensorboardX.SummaryWriter(log_dir=tb_path)

        def flow_loss(u, log_jacob, size_average=True):
            log_probs = (-0.5 * u.pow(2) - 0.5 * np.log(2 * np.pi)).sum()
            loss = -(log_probs + log_jacob)

            if size_average:
                loss /= u.size(0)
            return loss

        if self.args.resume_training:
            states = torch.load(os.path.join(self.args.run, 'logs', self.args.doc, 'checkpoint_epoch_530.pth'),
                                map_location=self.config.device)
            #import pdb
            #pdb.set_trace()
            net.load_state_dict(states[0])
            optimizer.load_state_dict(states[1])
            begin_epoch = states[2]
            step = states[3]
        else:
            step = 0
            begin_epoch = 0

        # Train the model
        for epoch in range(begin_epoch, self.config.training.n_epochs):
            self.adjust_learning_rate(optimizer, epoch)

            for batch_idx, (data, _) in enumerate(dataloader):
                net.train()
                # Transform to logit space since pixel values ranging from 0-1
                data = data.to(self.config.device) * 255. / 256.
                data += torch.rand_like(data) / 256.
                data = self.logit_transform(data)

                log_det_logit = F.softplus(-data).sum() + F.softplus(data).sum() + np.prod(
                    data.shape) * np.log(1 - 2 * self.config.data.lambda_logit)

                output, log_det = net(data)
                loss = flow_loss(output, log_det).mean()

                # Backward and optimize
                optimizer.zero_grad()
                loss.backward()

                #added clip_grad_norm
                clip_grad_norm_(net.parameters(), 100)
                #clip_grad_value_(net.parameters(), 0.01)


                optimizer.step()

                bpd = (loss.item() * data.shape[0] - log_det_logit) / (np.log(2) * np.prod(data.shape)) + 8

                # validation
                net.eval()
                with torch.no_grad():
                    try:
                        test_data, _ = next(test_iter)
                    except StopIteration:
                        test_iter = iter(test_loader)
                        test_data, _ = next(test_iter)

                    test_data = test_data.to(self.config.device) * 255. / 256.
                    test_data += torch.rand_like(test_data) / 256.
                    test_data = self.logit_transform(test_data)

                    test_log_det_logit = F.softplus(-test_data).sum() + F.softplus(test_data).sum() + np.prod(
                        test_data.shape) * np.log(1 - 2 * self.config.data.lambda_logit)

                    test_output, test_log_det = net(test_data)
                    test_loss = flow_loss(test_output, test_log_det).mean()
                    test_bpd = (test_loss.item() * test_data.shape[0] - test_log_det_logit) * (
                            1 / (np.log(2) * np.prod(test_data.shape))) + 8

                tb_logger.add_scalar('training_loss', loss, global_step=step)
                tb_logger.add_scalar('training_bpd', bpd, global_step=step)
                tb_logger.add_scalar('test_loss', test_loss, global_step=step)
                tb_logger.add_scalar('test_bpd', test_bpd, global_step=step)

                if step % self.config.training.log_interval == 0:
                    logging.info(
                        "epoch: {}, batch: {}, training_loss: {}, test_loss: {}".format(epoch, batch_idx, loss.item(),
                                                                                        test_loss.item()))

                step += 1

            if (epoch + 1) % self.config.training.snapshot_interval == 0:
                states = [
                    net.state_dict(),
                    optimizer.state_dict(),
                    epoch + 1,
                    step
                ]
                torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc,
                                                'checkpoint_epoch_{}.pth'.format(epoch + 1)))
                torch.save(states, os.path.join(self.args.run, 'logs', self.args.doc, 'checkpoint.pth'))
