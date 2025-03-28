import torch
from care.common import operators
from typing import Callable, Optional

class HJILossFunction:
    """
    Loss function for Hamilton-Jacobi-Isaacs PDEs used in reachability analysis.
    """
    
    def __init__(
        self, 
        hamiltonian_fn: Optional[Callable] = None,
        min_with: str = 'none',
        reach_mode: str = 'backward',
        reach_aim: str = 'reach',
        set_type: str = 'set'
    ):
        """
        Initialize the HJI Loss Function.
        
        Args:
            hamiltonian_fn: Function to compute the Hamiltonian
            min_with: Type of min operation to use ('none', 'target')
            reach_mode: Direction of reachability analysis ('backward' or 'forward')
            reach_aim: Aim of reachability analysis ('reach' or 'avoid')
        """
        self.min_with = min_with
        self.reach_mode = reach_mode
        self.reach_aim = reach_aim
        self.set_type = set_type
        self._hamiltonian_fn = hamiltonian_fn
    
    def compute_hamiltonian(self, x, p, Abs: Callable = abs) -> torch.Tensor:
        """
        Compute the Hamiltonian for the system.
        
        Args:
            x: State variables
            p: Costate variables
            
        Returns:
            Hamiltonian value
        """
        if self._hamiltonian_fn is None:
            raise NotImplementedError(
                "No Hamiltonian function provided. Either pass a hamiltonian_fn to the constructor "
                "or override this method in a subclass."
            )
        return self._hamiltonian_fn(x, p, Abs)
    
    def compute_loss(self, model_output, gt):
        """Template method that implements the common loss computation pattern."""
        x = model_output['model_in']
        y = model_output['model_out']
        source_boundary_values = gt['source_boundary_values']
        dirichlet_mask = gt['dirichlet_mask']

        du, _ = operators.jacobian(y, x)
        dudt = du[..., 0, 0]
        dudx = du[..., 0, 1:]
        states = x[...,1:]

        ham = self.compute_hamiltonian(states, dudx)
        ham = self._apply_minimization_constraint(ham)
        ham = self._apply_reachability_logic(ham)
        
        diff_constraint_hom = dudt + ham
        if self.min_with == 'target':
            diff_constraint_hom = torch.max(diff_constraint_hom[:, :, None], y - source_boundary_values)

        dirichlet_loss = self._compute_dirichlet_loss(y, source_boundary_values, dirichlet_mask)
        return {'dirichlet': dirichlet_loss, 'diff_constraint_hom': torch.abs(diff_constraint_hom)}

    def _compute_dirichlet_loss(self, y, source_boundary_values, dirichlet_mask):
        """Compute Dirichlet boundary condition loss."""
        dirichlet = y[dirichlet_mask] - source_boundary_values[dirichlet_mask]
        return torch.abs(dirichlet)

    def _apply_minimization_constraint(self, ham):
        """Apply minimization constraint to Hamiltonian."""
        if self.set_type == 'tube':
            ham = torch.clamp(ham, max=0.0)
        return ham

    def _apply_reachability_logic(self, ham):
        """Apply reachability mode logic to Hamiltonian."""
        if self.reach_mode == 'backward':
            ham = -ham
        return ham
