import argparse
import os
import torch
import torch.nn as nn
import gym
import numpy as np
import matplotlib.pyplot as plt
from torch.utils.data import Dataset, DataLoader
import cv2
from model import Model, Test, MLP, CNN, CNN_MLP
from sprites_env.envs import sprites
from torchvision.utils import save_image
import torchvision
from torch.utils.tensorboard import SummaryWriter
import time
from dataset import *
from ppo import PPO

def train(model, batch, optimizer, decoder_optimizer):
    avg_loss = 0.0
    avg_decoded_loss = 0.0

    for obs, reward_targets in zip(batch['obs'], batch['rewards']):
        optimizer.zero_grad()
        reward_predicted = model(obs).squeeze()
        loss = model.criterion(reward_predicted, reward_targets)
        avg_loss += loss

        # loss.backward()
        # optimizer.step()
    
    avg_loss.backward(retain_graph=True)
    optimizer.step()

    for obs in batch['obs']:
        decoder_optimizer.zero_grad()
        encoded_img = model.encoder(obs[-1][None, None, :].detach().clone())
        decoded_img = model.decoder(encoded_img).squeeze() 
        decoded_loss = model.criterion(decoded_img, obs[-1])
        avg_decoded_loss += decoded_loss
                
        # decoded_loss.backward()
        # decoder_optimizer.step()

    avg_decoded_loss.backward()
    decoder_optimizer.step()

    l = len(batch['obs'])
    avg_loss = avg_loss / l
    avg_decoded_loss = avg_decoded_loss / l

    return avg_loss.item(), decoded_img[None, :], avg_decoded_loss.item()

def test(t_model, decoder_optimizer, batch):
    avg_decoded_loss = 0.0

    for obs, states in zip(batch['obs'], batch['states']):
        decoder_optimizer.zero_grad()
        decoded = t_model(states.repeat(1, 32))  # (N, 64) -> (N, 64, 64)
        decoded_loss = t_model.criterion(decoded, obs)
        avg_decoded_loss += decoded_loss
                
    avg_decoded_loss.backward()
    decoder_optimizer.step()

    l = len(batch['obs'])
    avg_decoded_loss = avg_decoded_loss / l

    return decoded_loss, decoded[-1][None, :]

# argument parser
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--learning_rate', type=float, default=1e-3)
    parser.add_argument('--image_resolution', type=int, default=64)
    parser.add_argument('--time_steps', type=int, default=5)
    parser.add_argument('--tasks', type=int, default=1)
    parser.add_argument('--conditioning_frames', type=int, default=2)
    parser.add_argument('--num_epochs', type=int, default=0)
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--env', type=str, default='Sprites-v0')
    parser.add_argument('--reward', type=str, default='follow')
    parser.add_argument('--dataset_length', type=int, default=200)   
    parser.add_argument('--total_timesteps', type=int, default=1_000_000) # The project description uses 5_000_000
    args = parser.parse_args()
    return args


def main():
    # parse arguments
    args = parse_args()

    f = args.conditioning_frames
    t = args.time_steps
    assert t > f

    log_dir = 'runs/num_epochs=' + str(args.num_epochs) + 'env=' + args.env + '_time_steps=' + str(t) + '_frames=' + str(f) + '_lr=' + str(args.learning_rate) + '_batch_size=' + str(args.batch_size) + '_reward=' + args.reward + ' ||' + time.strftime("%d-%m-%Y_%H-%M-%S")
    if not(os.path.exists(log_dir)):
        os.makedirs(log_dir)
    writer = SummaryWriter(log_dir=log_dir)

    # load data
    dl, traj_images, ground_truth = dataloader(args.image_resolution, t, args.batch_size, f, args.reward, args.dataset_length)

    # initialize the environment
    env = gym.make(args.env)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    traj_images = traj_images.to(device)

    model = Model(t, f+1, args.tasks, args.image_resolution, device).to(device)
    # make_dir()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    decoder_optimizer = torch.optim.Adam(model.decoder.parameters(), lr=args.learning_rate)
    train_loss = []
    train_decoded_loss = []
    # t_model = Test(f+1)
    # decoder_optimizer = torch.optim.Adam(t_model.parameters(), lr=args.learning_rate)

    for epoch in range(args.num_epochs):
        running_loss = 0.0
        running_decoded_loss = 0.0
        num_batch = 0
        for batch in dl:
            loss, decoded_img, decoded_loss = train(model, batch, optimizer, decoder_optimizer) # here it's assumed that there's only one task - fix it?
            running_loss += loss
            # decoded_loss, decoded_img = test(t_model, decoder_optimizer, batch)
            running_decoded_loss += decoded_loss
            num_batch += 1

        # print or store data
        running_loss = running_loss / num_batch
        print('Epoch: {} \tLoss: {:.6f}'.format(epoch, running_loss))
        train_loss.append(running_loss)

        running_decoded_loss = running_decoded_loss / num_batch
        # print('Epoch: {} \tLoss: {:.6f}'.format(epoch, running_decoded_loss)) # added
        train_decoded_loss.append(running_decoded_loss)

        writer.add_scalar('Loss/train', running_loss, epoch)
        writer.add_scalar('Loss/decoded', running_decoded_loss, epoch)

        if epoch % 5 == 0:           
            decoded_img = (decoded_img + 1.0) * 255.0 / 2.0 
            writer.add_image('decoded_epoch{}'.format(epoch), decoded_img.to(torch.uint8))
           
            # save_decod_img(decoded_img.unsqueeze(0).cpu().data, epoch)           

    # decode and generate images with respect to reward functions
    output = model.test_decode(traj_images)
    output = (output + 1.0) * 255 / 2.0    

    img = make_image_seq_strip([output[None, :, None].repeat(3, axis=2).astype(np.float32)], sep_val=255.0).astype(np.uint8)   
    writer.add_image('ground_truth', ground_truth)
    writer.add_image('test_decoded', img[0])

    # set hyperparameters for PPO
    hyperparameters = {
        'timesteps_per_batch': 2048,
                    'max_timesteps_per_episode': 200,
                    'gamma': 0.99,
                    'gae_lamda': 0.95,
                    'n_updates_per_iteration': 10,
                    'lr': 1e-3,
                    'clip': 0.2,
                    'render': True,
                    'render_every_i': 10
    }
   
    # Trains the RL model
    cnn = CNN().to(device)
    # ppo = PPO(MLP, env, writer, model.encoder, **hyperparameters)
    ppo = PPO(MLP, env, writer, cnn, **hyperparameters)
    # ppo = PPO(CNN_MLP, env, writer, **hyperparameters)
    # ppo = PPO(MLP, env, writer, **hyperparameters) # oracle

    # Train the PPO model with a specified total timesteps
    ppo.learn(total_timesteps=args.total_timesteps)
    writer.flush()


# create a directory to save the results
def make_dir():
    image_dir = 'Decoded_images'
    if not os.path.exists(image_dir):
        os.makedirs(image_dir)

# save the reconstructed images as generated by the model
def save_decod_img(img, epoch):
    img = img.view(-1, 64, 64)
    save_image(img, './Decoded_images/epoch{}.png'.format(epoch))

if __name__ == '__main__':
    main()
