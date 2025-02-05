from abc import ABC, abstractmethod
import torch
from ..common import operators

class HJILossFunction(ABC):
    """Base class for Hamilton-Jacobi-Isaacs loss functions."""
    
    def __init__(self, dataset, minWith='none', reachMode='backward', reachAim='reach'):
        self.dataset = dataset
        self.minWith = minWith
        self.reachMode = reachMode
        self.reachAim = reachAim

    def compute_loss(self, model_output, gt):
        """Template method that implements the common loss computation pattern."""
        source_boundary_values = gt['source_boundary_values']
        x = model_output['model_in']
        y = model_output['model_out']
        dirichlet_mask = gt['dirichlet_mask']
        batch_size = x.shape[1]

        du, _ = operators.jacobian(y, x)
        dudt = du[..., 0, 0]
        dudx = du[..., 0, 1:]

        ham = self.compute_hamiltonian(x, dudx)
        ham = self._apply_reachability_logic(ham)
        ham = self._apply_minimization_constraint(ham)

        if torch.all(dirichlet_mask):
            diff_constraint_hom = torch.Tensor([0])
        else:
            diff_constraint_hom = dudt + ham
            if self.minWith == 'target':
                diff_constraint_hom = torch.max(diff_constraint_hom[:, :, None], y - source_boundary_values)

        dirichlet_loss = self._compute_dirichlet_loss(y, source_boundary_values, dirichlet_mask, batch_size)
        return {'dirichlet': dirichlet_loss, 'diff_constraint_hom': torch.abs(diff_constraint_hom).sum()}

    def _compute_dirichlet_loss(self, y, source_boundary_values, dirichlet_mask, batch_size):
        """Compute Dirichlet boundary condition loss."""
        dirichlet = y[dirichlet_mask] - source_boundary_values[dirichlet_mask]
        return torch.abs(dirichlet).sum() * batch_size / 15e2

    def _apply_minimization_constraint(self, ham):
        """Apply minimization constraint to Hamiltonian."""
        if self.minWith == 'zero':
            ham = torch.clamp(ham, max=0.0)
        return ham

    def _apply_reachability_logic(self, ham):
        """Apply reachability mode logic to Hamiltonian."""
        if self.reachMode == 'backward':
            ham = -ham
        return ham

    @abstractmethod
    def compute_hamiltonian(self, x, dudx):
        """Compute the Hamiltonian. Must be implemented by subclasses."""
        pass