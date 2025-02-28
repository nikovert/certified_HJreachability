import torch
import torch.nn.functional as F
from tqdm.autonotebook import tqdm
import time
import numpy as np
from pathlib import Path
import logging
from typing import Callable, Optional
from .curriculum import Curriculum
from ..common.dataset import ReachabilityDataset

logger = logging.getLogger(__name__)

def train(model: torch.nn.Module, 
          dataset: ReachabilityDataset,  # Updated type hint
          epochs: int, 
          lr: float, 
          epochs_til_checkpoint: int, 
          model_dir: str, 
          loss_fn: Callable, 
          pretrain_percentage: float = 0.01,  # Added curriculum parameters
          time_min: float = 0.0,
          time_max: float = 1.0,
          clip_grad: bool = True, 
          validation_fn: Optional[Callable] = None, 
          start_epoch: int = 0,
                    device: Optional[torch.device] = None,
          use_amp: bool = True,
          l1_lambda: float = 1e-2,  # Changed default to 1e-4 for L1 regularization
          weight_decay: float = 1e-2,  # Changed default to 1e-5 for L2 regularization
          is_finetuning: bool = False,  # New parameter to indicate fine-tuning
          momentum: float = 0.9,  # New parameter for momentum during fine-tuning
          **kwargs
          ) -> None:
    """
    Train a model using curriculum learning for reachability problems.
    
    Args:
        model: Neural network model to train
        dataset: Dataset for training, must be a ReachabilityDataset instance
        epochs: Total number of training epochs
        lr: Learning rate for the Adam optimizer
        epochs_til_checkpoint: Number of epochs between saving checkpoints
        model_dir: Directory to save model checkpoints and tensorboard logs
        loss_fn: Loss function that takes model output and ground truth as input
        pretrain_percentage: Fraction of total epochs to spend in pretraining phase (0 to 1)
        time_min: Minimum time value for curriculum learning
        time_max: Maximum time value for curriculum learning
        clip_grad: Whether to clip gradients during training
        validation_fn: Optional function to run validation during checkpoints
        start_epoch: Epoch to start or resume training from
        device: Device to use for training (default: CUDA if available, else CPU)
        l1_lambda: L1 regularization strength
        weight_decay: L2 regularization strength
    
    Raises:
        TypeError: If dataset is not an instance of ReachabilityDataset
    
    Notes:
        - Uses curriculum learning with two phases:
          1. Pretraining phase: Trains on a subset of time values
          2. Curriculum phase: Gradually increases the time range
        - Saves checkpoints periodically and logs metrics to tensorboard
        - Supports automatic mixed precision training on CUDA devices
    """
    if not isinstance(dataset, ReachabilityDataset):
        raise TypeError(f"Dataset must be an instance of ReachabilityDataset, got {type(dataset)}")
    
    # Enable automatic mixed precision for CUDA devices
    use_amp = device.type == 'cuda'
    scaler = torch.amp.GradScaler() if use_amp else None  # Updated GradScaler import
    
    # Enable CUDA optimizations
    if device.type == 'cuda':
        torch.backends.cudnn.benchmark = True

    # Ensure model and data are on the correct device
    model = model.to(device)
    
    # Adjust optimizer settings based on whether we're fine-tuning
    if is_finetuning:
        # Use SGD with momentum for fine-tuning
        optim = torch.optim.SGD(
            model.parameters(),
            lr=lr,
            momentum=momentum,
            weight_decay=weight_decay * 0.1,  # Reduce regularization during fine-tuning
            **kwargs
        )
        # Create a learning rate scheduler for fine-tuning
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optim, patience=1000, cooldown=200)
        
        # Add custom learning rate logging callback
        def log_lr(optimizer):
            current_lr = optimizer.param_groups[0]['lr']
            logger.info(f"Learning rate adjusted to: {current_lr}")
            
        # Store the callback with the scheduler
        scheduler.log_lr = log_lr
    else:
        # Use Adam for initial training
        optim = torch.optim.Adam(
            model.parameters(),
            lr=lr,
            weight_decay=weight_decay,
            **kwargs
        )
        scheduler = None

    # Initialize curriculum
    curriculum = Curriculum(
        dataset=dataset,
        pretrain_percentage=pretrain_percentage,
        total_steps=epochs,
        time_min=time_min,
        time_max=time_max,
        rollout=not is_finetuning  # Disable rollout during fine-tuning
    )

    # Load the checkpoint if required
    if start_epoch > 0:
        try:
            model_path = Path(model_dir) / 'checkpoints' / f'model_epoch_{start_epoch:04d}.pth'
            model.load_checkpoint(model_path)
            model.train()
            logger.info(f"Loaded checkpoint from epoch {start_epoch}")
        except FileNotFoundError:
            logger.error(f"Checkpoint file not found: {model_path}")
        except Exception as e:
            logger.error(f"Error loading checkpoint: {e}")

    # Make sure all path operations use Path consistently
    model_dir = Path(model_dir)
    checkpoints_dir = model_dir / 'checkpoints'

    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    model.checkpoint_dir = checkpoints_dir  # Set checkpoint directory for model

    with tqdm(total=epochs) as pbar:
        train_losses = []
        best_loss = float('inf')
        patience_counter = 0
        
        for epoch in range(start_epoch, epochs): 
            start_time = time.time()
            # Update curriculum scheduler epoch at the start of each epoch
            curriculum.step()
            
            if epoch % epochs_til_checkpoint == 0 and epoch > 0:
                # Save periodic checkpoint using model's method
                model.save_checkpoint(
                    name='model_current',
                    optimizer=optim,
                    epoch=epoch
                )
                
                # Save losses separately for analysis
                np.savetxt(checkpoints_dir / f'train_losses_epoch_{epoch:04d}.txt',
                          np.array(train_losses))
                train_losses = []
                _, t_max = curriculum.get_time_range()
                if validation_fn is not None:
                    validation_fn(model, checkpoints_dir, epoch, t_max=t_max)

                if device.type == 'cuda':
                    torch.cuda.empty_cache()

            # Get a fresh batch of data
            model_input, gt = dataset.get_batch()
            
            # Ensure coords requires gradients
            model_input['coords'].requires_grad_(True)
            
            optim.zero_grad(set_to_none=True)
            
            with torch.autocast(device.type, enabled=use_amp):
                model_output = model(model_input)
                losses = loss_fn(model_output, gt)
                
                # Get weights from curriculum
                batch_size = model_input['coords'].shape[0]  # Assuming first dimension is batch size
                loss_weights = curriculum.get_loss_weights(batch_size)
                
                # Apply weights to losses and normalize by batch size
                train_loss = sum(loss.mean() * loss_weights.get(name, 1.0)
                                for name, loss in losses.items())
                
                # Calculate total loss and add L1 regularization using PyTorch's built-in function
                if l1_lambda > 0:
                    l1_loss = torch.tensor(0., device=device)
                    for param in model.parameters():
                        l1_loss += F.l1_loss(param, torch.zeros_like(param), reduction='sum')
                    train_loss += l1_lambda * l1_loss

            if scaler is not None:
                scaler.scale(train_loss).backward()
                if clip_grad:
                    scaler.unscale_(optim)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                scaler.step(optim)
                scaler.update()
            else:
                train_loss.backward()
                if clip_grad:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optim.step()

            train_losses.append(train_loss.item())

            # # Report progress every 100 epochs or 1/1000th of total epochs, whichever is smaller
            if epoch % max(100,(epochs //1000)) == 0:
                tqdm.write(f"Epoch {epoch}, Total Loss: {train_loss:.6f},"
                          f"L1 Reg: {(l1_lambda * l1_loss if l1_lambda > 0 else 0):.6f}, "
                          f"L2 Reg: {(weight_decay * sum((p ** 2).sum() for p in model.parameters())):.6f}, "
                          f"Time: {time.time() - start_time:.3f}s")
                curr_progress = curriculum.get_progress()
                t_min, t_max = curriculum.get_time_range()
                phase = "Pretraining" if curriculum.is_pretraining else "Curriculum"
                tqdm.write(f"{phase} - Progress: {curr_progress:.2%}, Time range: [{t_min:.3f}, {t_max:.3f}]")

            pbar.update(1)

            # Learning rate scheduling for fine-tuning
            if is_finetuning and scheduler is not None:
                prev_lr = optim.param_groups[0]['lr']
                scheduler.step(train_loss)
                # Log if learning rate changed
                if prev_lr != optim.param_groups[0]['lr']:
                    scheduler.log_lr(optim)
                
                # Early stopping for fine-tuning
                if train_loss < best_loss:
                    best_loss = train_loss
                    patience_counter = 0
                    # Save best model during fine-tuning
                    model.save_checkpoint(
                        name='model_best_finetuned',
                        optimizer=optim,
                        epoch=epoch
                    )
                else:
                    patience_counter += 1
                    if patience_counter >= 10000:  # Early stopping after 10000 epochs without improvement
                        logger.info("Early stopping triggered")
                        break

        # Save final model
        model.save_checkpoint(
            name='model_final',
            optimizer=optim,
            epoch=epochs,
            training_completed=True
        )

        # Save final losses
        np.savetxt(checkpoints_dir / 'train_losses_final.txt', 
                   np.array(train_losses))

    # After training, load the best model if we were fine-tuning
    if is_finetuning:
        try:
            best_model_path = Path(model_dir) / 'checkpoints' / 'model_best_finetuned.pth'
            if best_model_path.exists():
                model.load_checkpoint(best_model_path)
                logger.info("Loaded best fine-tuned model")
        except Exception as e:
            logger.warning(f"Could not load best fine-tuned model: {e}")
