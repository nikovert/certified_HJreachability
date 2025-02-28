import logging
from typing import Dict
from certreach.common.dataset import ReachabilityDataset

logger = logging.getLogger(__name__)

class Curriculum:
    """Manages curriculum learning progress for ReachabilityDataset."""
    DIRICHLET_WEIGHT = 1/15e2
    def __init__(self, 
                 dataset: ReachabilityDataset,
                 pretrain_percentage: int,
                 total_steps: int,
                 time_min: float = 0.0,
                 time_max: float = 1.0,
                 rollout: bool = True):
        if not isinstance(dataset, ReachabilityDataset):
            raise TypeError(f"Dataset must be ReachabilityDataset, got {type(dataset)}")
            
        self.dataset = dataset
        self.pretrain_percentage = pretrain_percentage
        self.total_steps = total_steps
        self.pretrain_steps = self.pretrain_percentage * self.total_steps
        self.time_min = time_min
        self.time_max = time_max
        self.current_step = 0
        self.rollout = rollout
    
    def __len__(self):
        return self.total_steps

    def step(self):
        """Update curriculum progress and dataset time range."""
        self.current_step += 1
        t_min, t_max = self.get_time_range()
        self.dataset.update_time_range(t_min, t_max)
    
    def get_progress(self) -> float:
        """Get current curriculum progress."""
        if not self.rollout:
            return 1.0
        if self.current_step < self.pretrain_steps:
            return 0.0
        else:
            progress = 2*(self.current_step/self.total_steps - self.pretrain_percentage)/(1 - self.pretrain_percentage)
            return min(progress, 1.0)

    def get_time_range(self) -> tuple[float, float]:
        """Get current time range based on curriculum progress."""
        progress = self.get_progress()
        current_max = self.time_min + (self.time_max - self.time_min) * progress
        return self.time_min, current_max
            
    @property
    def is_pretraining(self) -> bool:
        return self.get_progress() == 0.0
    
    def get_loss_weights(self, batch_size) -> Dict[str, float]:
        """
        Returns weights for different loss components based on current curriculum state.
        
        Returns:
            Dict[str, float]: Dictionary mapping loss names to their weights
        """
        
        if self.is_pretraining:
            # During pretraining, focus more on Dirichlet boundary conditions
            weights = {
                'dirichlet': batch_size*self.DIRICHLET_WEIGHT,
                'diff_constraint_hom': 1.0
            }
        else:
            # Gradually increase importance of homogeneous differential constraint
            weights = {
                'dirichlet': batch_size*self.DIRICHLET_WEIGHT,
                'diff_constraint_hom': 1.0
            }
        
        return weights
