#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6

import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
import random
import copy
from utils import average_weights
from node import *


class DatasetSplit(Dataset):
    """An abstract Dataset class wrapped around Pytorch Dataset class.
    """

    def __init__(self, dataset, idxs):
        self.dataset = dataset
        self.idxs = [int(i) for i in idxs]

    def __len__(self):
        return len(self.idxs)

    def __getitem__(self, item):
        image, label = self.dataset[self.idxs[item]]
        return torch.tensor(image), torch.tensor(label)


class LocalUpdate(object):
    def __init__(self, args, dataset, idxs, logger=None,attacker=False):
        self.args = args
        self.logger = logger
        self.trainloader, self.validloader, self.testloader = self.train_val_test(
            dataset, list(idxs),attacker)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # Default criterion set to NLL loss function
        self.criterion = nn.NLLLoss().to(self.device)
        #Add grads dictionary
        self.grads = {}

    def train_val_test(self, dataset, idxs, attacker):
        """
        Returns train, validation and test dataloaders for a given dataset
        and user indexes.
        """
        #Spoil data for malicous users (attackers)
        if attacker:
            attack_offset = random.randint(1,9)
            #Label flipping (increment by one) for cifar/mnist dataset
            for i in idxs:
                dataset[i][1] = (dataset[i][1] + attack_offset) % 10

        # split indexes for train, validation, and test (80, 10, 10)
        idxs_train = idxs[:int(0.8*len(idxs))]
        idxs_val = idxs[int(0.8*len(idxs)):int(0.9*len(idxs))]
        idxs_test = idxs[int(0.9*len(idxs)):]

        trainloader = DataLoader(DatasetSplit(dataset, idxs_train),
                                 batch_size=self.args.local_bs, shuffle=True)
        validloader = DataLoader(DatasetSplit(dataset, idxs_val),
                                 batch_size=int(len(idxs_val)/10), shuffle=False)
        testloader = DataLoader(DatasetSplit(dataset, idxs_test),
                                batch_size=int(len(idxs_test)/10), shuffle=False)
        return trainloader, validloader, testloader

    def update_weights(self, model, global_round):
        # Set mode to train model
        model.train()
        epoch_loss = []

        #Extract gradients
        for name, param in model.named_parameters():
            self.grads[name.split(".")[0]] = []

        # Set optimizer for the local updates
        if self.args.optimizer == 'sgd':
            optimizer = torch.optim.SGD(model.parameters(), lr=self.args.lr,
                                        momentum=0.5)
        elif self.args.optimizer == 'adam':
            optimizer = torch.optim.Adam(model.parameters(), lr=self.args.lr,
                                         weight_decay=1e-4)

        for iter in range(self.args.local_ep):
            batch_loss = []
            for batch_idx, (images, labels) in enumerate(self.trainloader):
                images, labels = images.to(self.device), labels.to(self.device)

                model.zero_grad()
                log_probs = model(images)
                loss = self.criterion(log_probs, labels)
                loss.backward()
                #Collect grads here!
                if iter == self.args.local_ep - 1:
                    model.stash_grads()
                    self.grads = copy.deepcopy(model.grads)
                optimizer.step()

                if self.args.verbose and (batch_idx % 10 == 0):
                    print('| Global Round : {} | Local Epoch : {} | [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                        global_round, iter, batch_idx * len(images),
                        len(self.trainloader.dataset),
                        100. * batch_idx / len(self.trainloader), loss.item()))
                #self.logger.add_scalar('loss', loss.item())
                batch_loss.append(loss.item())
            epoch_loss.append(sum(batch_loss)/len(batch_loss))

        return model.state_dict(), sum(epoch_loss) / len(epoch_loss), self.grads

    def inference(self, model):
        """ Returns the inference accuracy and loss.
        """

        model.eval()
        loss, total, correct = 0.0, 0.0, 0.0

        for batch_idx, (images, labels) in enumerate(self.testloader):
            images, labels = images.to(self.device), labels.to(self.device)

            # Inference
            outputs = model(images)
            batch_loss = self.criterion(outputs, labels)
            loss += batch_loss.item()

            # Prediction
            _, pred_labels = torch.max(outputs, 1)
            pred_labels = pred_labels.view(-1)
            correct += torch.sum(torch.eq(pred_labels, labels)).item()
            total += len(labels)

        accuracy = correct/total
        return accuracy, loss


def test_inference(args, model, test_dataset):
    """ Returns the test accuracy and loss.
    """

    model.eval()
    loss, total, correct = 0.0, 0.0, 0.0

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    criterion = nn.NLLLoss().to(device)
    testloader = DataLoader(test_dataset, batch_size=128,
                            shuffle=False)

    for batch_idx, (images, labels) in enumerate(testloader):
        images, labels = images.to(device), labels.to(device)

        # Inference
        outputs = model(images)
        batch_loss = criterion(outputs, labels)
        loss += batch_loss.item()

        # Prediction
        _, pred_labels = torch.max(outputs, 1)
        pred_labels = pred_labels.view(-1)
        correct += torch.sum(torch.eq(pred_labels, labels)).item()
        total += len(labels)

    accuracy = correct/total
    return accuracy, loss

def ripple_updates(adj_list,global_epoch,colors,dir_path=None,NON_IID_FRAC=None,CLUMP_STRATEGY="static",OPPOSIT_STRATEGY="random"):
    
    #If clumping applied
    if CLUMP_STRATEGY == "dynamic":
        #Pick next candidates
        for node in adj_list:
            node.next_candidates()

    #Ripple updates with 1-hop neighbors
    for node in adj_list:
        local_weights = []
        for neighbor in node.neighbors:
            local_weights.append(copy.deepcopy(neighbor.model.state_dict()))
        neighbors_weight = average_weights(local_weights)
        aggregate_weight = average_weights([node.model.state_dict(),neighbors_weight])
        node.model.load_state_dict(aggregate_weight)

    #If clumping applied
    if CLUMP_STRATEGY == "dynamic":
        #Update next neighbors
        for node in adj_list:
            node.next_peers(non_iid_frac=NON_IID_FRAC,non_iid_strategy=OPPOSIT_STRATEGY)
        
        #Draw round graph
        graph = build_graph(adj_list,nx.DiGraph())
        fname = dir_path + "G" + str(global_epoch + 1) + ".png" if dir_path != None else "G" + str(global_epoch + 1) + ".png"
        draw_graph(graph,fname,colors)