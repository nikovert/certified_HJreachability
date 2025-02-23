import os
import torch
import logging
import numpy as np
import torch.multiprocessing as mp
from typing import Optional
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from certreach.common.dataset import ReachabilityDataset
from certreach.learning.training import train
from certreach.learning.networks import SingleBVPNet
from certreach.verification.symbolic import extract_symbolic_model
from certreach.verification.dreal_utils import (
    extract_dreal_partials,
    process_dreal_result
)
from certreach.verification.verify import verify_system
from certreach.common.matlab_loader import load_matlab_data, compare_with_nn

from .verification import dreal_triple_integrator_BRS
from .loss import initialize_loss
from examples.utils.experiment_utils import get_experiment_folder, save_experiment_details
from examples.factories import register_example

# Set multiprocessing start method
mp.set_start_method('spawn', force=True)

@register_example
class TripleIntegrator:
    Name = "triple_integrator"

    def __init__(self, args):
        # ...similar to DoubleIntegrator...
        self.args = args
        self.root_path = get_experiment_folder(args.logging_root, self.Name)
        self.logger = logging.getLogger(__name__)
        self.device = torch.device(args.device)
        self.model = None
        self.dataset = None
        self.loss_fn = None

    def initialize_components(self):
        if self.dataset is None:
            def triple_integrator_boundary(coords):
                pos = coords[:, 1:4]  # Extract [x1, x2, x3]
                boundary_values = torch.norm(pos, dim=1, keepdim=True)
                return boundary_values - 0.25

            self.dataset = ReachabilityDataset(  # Changed from BaseReachabilityDataset
                numpoints=self.args.numpoints,
                tMin=self.args.tMin,
                tMax=self.args.tMax,
                pretrain=self.args.pretrain,
                pretrain_iters=self.args.pretrain_iters,
                counter_start=self.args.counter_start,
                counter_end=self.args.counter_end,
                num_src_samples=self.args.num_src_samples,
                seed=self.args.seed,
                device=self.device,
                num_states=3,  # [position, velocity, acceleration]
                compute_boundary_values=triple_integrator_boundary
            )

        if self.model is None:
            self.model = SingleBVPNet(
                in_features=self.args.in_features,
                out_features=self.args.out_features,
                type=self.args.model_type,
                mode=self.args.model_mode,
                hidden_features=self.args.num_nl,
                num_hidden_layers=self.args.num_hl,
                use_polynomial=self.args.use_polynomial,
                poly_degree=self.args.poly_degree
            ).to(self.device)

        if self.loss_fn is None:
            self.loss_fn = initialize_loss(
                self.dataset,
                minWith=self.args.minWith,
                reachMode=self.args.reachMode,
                reachAim=self.args.reachAim
            )

    def train(self, counterexample: Optional[torch.Tensor] = None):
        self.logger.info("Initializing training components")
        
        if counterexample is not None:
            if not isinstance(counterexample, torch.Tensor):
                raise TypeError("counterexample must be a torch.Tensor")
            
            def triple_integrator_boundary(coords):
                pos = coords[:, 1:4]  # Extract [x1, x2, x3]
                boundary_values = torch.norm(pos, dim=1, keepdim=True)
                return boundary_values - 0.25

            self.dataset = ReachabilityDataset(  # Changed from BaseReachabilityDataset
                numpoints=self.args.numpoints,
                tMin=self.args.tMin,
                tMax=self.args.tMax,
                pretrain=self.args.pretrain,
                pretrain_iters=self.args.pretrain_iters,
                counter_start=self.args.counter_start,
                counter_end=self.args.counter_end,
                num_src_samples=self.args.num_src_samples,
                seed=self.args.seed,
                device=self.device,
                counterexample=counterexample,
                percentage_in_counterexample=20,
                percentage_at_t0=20,
                epsilon_radius=self.args.epsilon_radius,
                num_states=3,
                compute_boundary_values=triple_integrator_boundary
            )
        else:
            self.initialize_components()

        dataloader = torch.utils.data.DataLoader(
            self.dataset,
            batch_size=self.args.batch_size,
            shuffle=True,
            pin_memory=False
        )
        
        self.logger.info("Starting model training")
        train(
            model=self.model,
            train_dataloader=dataloader,
            epochs=self.args.num_epochs,
            lr=self.args.lr,
            steps_til_summary=100,
            epochs_til_checkpoint=1000,
            model_dir=self.root_path,
            loss_fn=self.loss_fn,
            clip_grad=False,
            use_lbfgs=False,
            validation_fn=self.validate,
            start_epoch=0
        )

        self.logger.info("Saving experiment details")
        save_experiment_details(self.root_path, str(self.loss_fn), vars(self.args))

    def validate(self, model, ckpt_dir, epoch):
        """Validation function for triple integrator"""
        state_range = torch.linspace(-1.5, 1.5, 50)
        times = [self.args.tMin, 0.5 * (self.args.tMin + self.args.tMax), self.args.tMax]
        num_times = len(times)

        fig = plt.figure(figsize=(15, 5 * num_times))
        grid = plt.GridSpec(num_times, 3, figure=fig)

        for t_idx, t in enumerate(times):
            X1, X2, X3 = torch.meshgrid(state_range, state_range, state_range, indexing='ij')
            coords = torch.cat((
                torch.ones_like(X1.reshape(-1, 1)) * t,
                X1.reshape(-1, 1),
                X2.reshape(-1, 1),
                X3.reshape(-1, 1)
            ), dim=1).to(self.device)

            model_out = model({'coords': coords})['model_out'].detach().cpu().numpy()
            model_out = model_out.reshape(X1.shape)

            slices = [(0, 1, 25), (1, 2, 25), (0, 2, 25)]
            titles = ['Position-Velocity', 'Velocity-Acceleration', 'Position-Acceleration']
            
            for idx, (i, j, k) in enumerate(slices):
                ax = fig.add_subplot(grid[t_idx, idx])
                slice_data = model_out[:, :, k]
                contour = ax.contourf(state_range, state_range, slice_data, levels=50, cmap='bwr')
                ax.set_title(f"{titles[idx]} at t={t:.2f}")
                fig.colorbar(contour, ax=ax)

        plt.tight_layout()
        fig.savefig(os.path.join(ckpt_dir, f'TripleIntegrator_val_epoch_{epoch:04d}.png'))
        plt.close(fig)

    def plot_final_model(self, model, save_dir, epsilon, save_file="Final_Model_Comparison_With_Zero_Set.png"):
        """Plot comparison of triple integrator value functions"""
        state_range = torch.linspace(-1, 1, 50)
        slice_pos = 25

        fig, axes = plt.subplots(2, 3, figsize=(18, 12))
        fig.suptitle(f"Triple Integrator Value Function Comparison (ε={epsilon})")

        X1, X2, X3 = torch.meshgrid(state_range, state_range, state_range, indexing='ij')
        coords = torch.cat((
            torch.ones_like(X1.reshape(-1, 1)) * self.args.tMax,
            X1.reshape(-1, 1),
            X2.reshape(-1, 1),
            X3.reshape(-1, 1)
        ), dim=1).to(self.device)

        model_out = model({'coords': coords})['model_out'].cpu().detach().numpy().reshape(X1.shape)
        adjusted_model_out = model_out - epsilon

        slices = [(0, 1), (1, 2), (0, 2)]
        titles = ['Position-Velocity', 'Velocity-Acceleration', 'Position-Acceleration']

        for idx, (i, j) in enumerate(slices):
            # Original
            contour1 = axes[0, idx].contourf(state_range, state_range, 
                                           model_out[:, :, slice_pos], levels=50, cmap='bwr')
            axes[0, idx].set_title(f"Original: {titles[idx]}")
            fig.colorbar(contour1, ax=axes[0, idx])

            # Epsilon-adjusted
            contour2 = axes[1, idx].contourf(state_range, state_range,
                                           adjusted_model_out[:, :, slice_pos], levels=50, cmap='bwr')
            axes[1, idx].set_title(f"ε-Adjusted: {titles[idx]}")
            fig.colorbar(contour2, ax=axes[1, idx])

        plt.tight_layout()
        plt.savefig(os.path.join(save_dir, save_file))
        plt.close(fig)

    def load_model(self, model_path):
        if self.model is None:
            self.initialize_components()
        
        state_dict = torch.load(model_path, map_location=self.device)
        self.model.load_state_dict(state_dict)
        self.model.to(self.device)
