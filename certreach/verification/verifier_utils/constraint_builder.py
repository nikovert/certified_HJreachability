"""
Utility functions for building and serializing dReal constraints across processes.
This module provides mechanisms to rebuild complex mathematical expressions from serialized forms.
"""

import logging
import dreal
from dreal import Variable, And, CheckSatisfiability
from typing import Dict, List, Any, Tuple, Optional, Callable
import time
import multiprocessing as mp

logger = logging.getLogger(__name__)

def parse_expression(expr_str: str, variables: Dict[str, Variable]) -> Any:
    """
    Parse a string representation of a dReal expression and rebuild it.
    This is a simplified parser for demonstration purposes.
    
    Args:
        expr_str: String representation of a dReal expression
        variables: Dictionary mapping variable names to dReal Variable objects
    
    Returns:
        Rebuilt dReal expression
    """
    # This would be a much more complex parser in a real implementation
    # For now, we'll handle some simple cases
    
    # Replace variable names with their dReal Variable objects
    for var_name, _ in variables.items():
        expr_str = expr_str.replace(var_name, f"variables['{var_name}']")
    
    # Handle common operators and functions
    expr_str = expr_str.replace("sin", "dreal.sin")
    expr_str = expr_str.replace("cos", "dreal.cos")
    expr_str = expr_str.replace("exp", "dreal.exp")
    expr_str = expr_str.replace("tanh", "dreal.tanh")
    expr_str = expr_str.replace("Max", "dreal.Max")
    expr_str = expr_str.replace("Min", "dreal.Min")
    
    try:
        # Evaluate the expression string to rebuild it
        # Use eval with the necessary context
        context = {"dreal": dreal, "variables": variables}
        return eval(expr_str, context)
    except Exception as e:
        logger.error(f"Error parsing expression '{expr_str}': {e}")
        return None

def rebuild_constraint(constraint_type: str, 
                      variables: Dict[str, Variable],
                      value_fn_expr: str,
                      partials_expr: Dict[str, str],
                      boundary_fn_expr: str,
                      hamiltonian_expr: Callable,
                      epsilon: float,
                      is_initial_time: bool = False) -> dreal.Formula:
    """
    Rebuild a dReal constraint from its components.
    
    Args:
        constraint_type: Type of constraint ('boundary_1', 'derivative_1', etc.)
        variables: Dictionary of dReal Variables
        value_fn_expr: String representation of value function
        partials_expr: Dictionary of partial derivative strings
        boundary_fn_expr: String representation of boundary function
        hamiltonian_expr: String representation of the Hamiltonian
        epsilon: Verification tolerance
        is_initial_time: Whether this is an initial time constraint
        
    Returns:
        Rebuilt dReal Formula constraint
    """
    try:
        # Extract time and state variables
        t = variables.get("x_1_1")
        state_vars = []
        partials = []
        
        for i in range(2, len(variables) + 1):
            var_name = f"x_1_{i}"
            if var_name in variables:
                state_vars.append(variables[var_name])
        
        # Parse expressions
        value_fn = parse_expression(value_fn_expr, variables)
        boundary_value = parse_expression(boundary_fn_expr, variables)
        
        # Get partial derivatives
        dv_dt = parse_expression(partials_expr.get(f"partial_x_1_1", "0"), variables)
        for i in range(2, len(variables) + 1):
            partial_name = f"partial_x_1_{i}"
            if partial_name in partials_expr:
                partials.append(parse_expression(partials_expr[partial_name], variables))
        
        hamiltonian_value = parse_expression(hamiltonian_expr, variables)
        
        # Define state constraints
        if is_initial_time:
            time_constraint = (t == 0)
        else:
            time_constraint = And(t >= 0, t <= 1)
            
        space_constraints = [And(var >= -1, var <= 1) for var in state_vars]
        
        # Build the specified constraint
        if constraint_type == 'boundary_1':
            return And(time_constraint, *space_constraints, value_fn - boundary_value > epsilon)
        elif constraint_type == 'boundary_2':
            return And(time_constraint, *space_constraints, value_fn - boundary_value < -epsilon)
        elif constraint_type == 'derivative_1':
            return And(time_constraint, *space_constraints, dv_dt + hamiltonian_value < -epsilon)
        elif constraint_type == 'derivative_2':
            return And(time_constraint, *space_constraints, dv_dt + hamiltonian_value > epsilon)
        elif constraint_type == 'target_1':
            return And(time_constraint, *space_constraints, 
                      And(dv_dt + hamiltonian_value < -epsilon, value_fn - boundary_value < -epsilon))
        elif constraint_type == 'target_2':
            return And(time_constraint, *space_constraints, dv_dt + hamiltonian_value > epsilon)
        elif constraint_type == 'target_3':
            return And(time_constraint, *space_constraints, value_fn - boundary_value > epsilon)
        else:
            logger.error(f"Unknown constraint type: {constraint_type}")
            return None
            
    except Exception as e:
        logger.error(f"Error building constraint: {e}")
        return None

def process_check_advanced(constraint_data, hamiltonian_expr, value_fn_expr, boundary_fn_expr, partials_expr) -> Tuple[int, Optional[str]]:
    """
    Advanced process-compatible function to check a constraint by recreating all necessary components.
    
    Args:
        constraint_data: Dictionary with constraint information
        hamiltonian_expr: Serialized string representation of the Hamiltonian
        value_fn_expr: Serialized string representation of value function expression
        boundary_fn_expr: Serialized string representation of boundary function expression
        partials_expr: Dictionary of serialized partial derivative expressions
        
    Returns:
        Tuple of (constraint_id, result_string)
    """
    try:
        # Extract basic constraint data
        constraint_id = constraint_data['constraint_id']
        constraint_type = constraint_data['constraint_type']
        epsilon = constraint_data['epsilon']
        delta = constraint_data['delta']
        is_initial_time = constraint_data['is_initial_time']
        
        # Create variables
        time_var = Variable("x_1_1")
        state_vars = [Variable(f"x_1_{i+2}") for i in range(len(constraint_data['space_constraints']))]
        
        # Create dictionary of all variables
        variables = {"x_1_1": time_var}
        for i, var in enumerate(state_vars):
            variables[f"x_1_{i+2}"] = var
        
        # Use rebuild_constraint to create the constraint
        constraint = rebuild_constraint(
            constraint_type=constraint_type,
            variables=variables,
            value_fn_expr=value_fn_expr,
            partials_expr=partials_expr,
            boundary_fn_expr=boundary_fn_expr,
            hamiltonian_expr=hamiltonian_expr,
            epsilon=epsilon,
            is_initial_time=is_initial_time
        )
        
        if constraint is None:
            logger.error(f"Failed to build constraint {constraint_id}")
            return constraint_id, f"Error: Failed to build constraint {constraint_type}"
        
        # Check constraint
        proc_name = mp.current_process().name
        logger.debug(f"Process {proc_name} checking constraint {constraint_id}: {constraint_type}")
        
        start_time = time.monotonic()
        
        # Execute the constraint check
        result = CheckSatisfiability(constraint, delta)
        check_time = time.monotonic() - start_time
        
        if result:
            logger.info(f"Process {proc_name} found counterexample for constraint {constraint_id} in {check_time:.4f}s")
            return constraint_id, str(result)
        else:
            logger.info(f"Process {proc_name} found no counterexample for constraint {constraint_id} in {check_time:.4f}s")
            return constraint_id, None
            
    except Exception as e:
        logger.error(f"Process {mp.current_process().name} error: {e}")
        return constraint_data.get('constraint_id', -1), f"Error: {str(e)}"

def serialize_dreal_expression(expr) -> str:
    """
    Convert a dReal expression to a serializable string format.
    
    Args:
        expr: dReal expression
        
    Returns:
        String representation of the expression
    """
    return str(expr)

def create_constraint_data(constraint_id: int,
                          constraint_type: str,
                          is_initial_time: bool,
                          state_dim: int,
                          epsilon: float,
                          delta: float,
                          reach_mode: str = 'forward',
                          set_type: str = 'set',
                          time_range: Tuple[float, float] = (0.0, 1.0),
                          space_range: Tuple[float, float] = (-1.0, 1.0)) -> Dict:
    """
    Create a serializable constraint data dictionary for process-based checking.
    
    Args:
        constraint_id: Unique identifier for the constraint
        constraint_type: Type of constraint ('boundary_1', 'derivative_1', etc.)
        is_initial_time: Whether this constraint is for initial time (t=0)
        state_dim: Dimension of state space
        epsilon: Verification tolerance
        delta: dReal precision parameter
        reach_mode: 'forward' or 'backward'
        set_type: 'set' or 'tube'
        time_range: Range for time variable
        space_range: Range for state variables (same for all dimensions)
        
    Returns:
        Dictionary with constraint data
    """
    # If initial time, adjust time range
    if is_initial_time:
        time_range = (0.0, 0.0)
    
    # Create space constraints for all dimensions
    space_constraints = [space_range] * state_dim
    
    return {
        'constraint_id': constraint_id,
        'constraint_type': constraint_type,
        'time_constraint': time_range,
        'space_constraints': space_constraints,
        'epsilon': epsilon,
        'delta': delta,
        'is_initial_time': is_initial_time,
        'reach_mode': reach_mode,
        'set_type': set_type
    }

def prepare_constraint_data_batch(state_dim: int, 
                                epsilon: float, 
                                delta: float,
                                min_with: str = 'none',
                                reach_mode: str = 'forward',
                                set_type: str = 'set',
                                time_subdivisions: int = 4) -> List[Dict]:
    """
    Prepare a batch of constraint data objects for parallel checking.
    Non-initial time constraints are divided into multiple constraints over subintervals.
    
    Args:
        state_dim: Dimension of state space
        epsilon: Verification tolerance
        delta: dReal precision parameter
        min_with: 'none' or 'target'
        reach_mode: 'forward' or 'backward'
        set_type: 'set' or 'tube'
        time_subdivisions: Number of time subintervals to create (default: 1, no subdivision)
        
    Returns:
        List of constraint data dictionaries
    """
    constraint_data_objects = []
    
    # Ensure at least 1 subdivision
    time_subdivisions = max(1, time_subdivisions)
    
    # Create time ranges for subdivisions
    time_ranges = []
    if time_subdivisions > 1:
        step = 1.0 / time_subdivisions
        for i in range(time_subdivisions):
            start = i * step
            end = (i + 1) * step
            time_ranges.append((start, end))
    else:
        time_ranges = [(0.0, 1.0)]  # Default full time horizon
    
    # Counter for unique constraint IDs
    constraint_id = 1
    
    if min_with == 'target':
        # For non-initial time constraints: target_1, target_2, target_3
        for time_range in time_ranges:
            constraint_data_objects.append(
                create_constraint_data(constraint_id, 'target_1', False, state_dim, epsilon, delta, 
                                      reach_mode, set_type, time_range)
            )
            constraint_id += 1
            
            constraint_data_objects.append(
                create_constraint_data(constraint_id, 'target_2', False, state_dim, epsilon, delta, 
                                      reach_mode, set_type, time_range)
            )
            constraint_id += 1
            
            constraint_data_objects.append(
                create_constraint_data(constraint_id, 'target_3', False, state_dim, epsilon, delta,
                                      reach_mode, set_type, time_range)
            )
            constraint_id += 1
            
        # Initial time constraint: boundary_2
        constraint_data_objects.append(
            create_constraint_data(constraint_id, 'boundary_2', True, state_dim, epsilon, delta, 
                                  reach_mode, set_type)
        )
    else:
        # For non-initial time constraints: derivative_1, derivative_2
        for time_range in time_ranges:
            constraint_data_objects.append(
                create_constraint_data(constraint_id, 'derivative_1', False, state_dim, epsilon, delta, 
                                      reach_mode, set_type, time_range)
            )
            constraint_id += 1
            
            constraint_data_objects.append(
                create_constraint_data(constraint_id, 'derivative_2', False, state_dim, epsilon, delta, 
                                      reach_mode, set_type, time_range)
            )
            constraint_id += 1
            
        # Initial time constraints: boundary_1, boundary_2
        constraint_data_objects.append(
            create_constraint_data(constraint_id, 'boundary_1', True, state_dim, epsilon, delta, 
                                  reach_mode, set_type)
        )
        constraint_id += 1
        
        constraint_data_objects.append(
            create_constraint_data(constraint_id, 'boundary_2', True, state_dim, epsilon, delta, 
                                  reach_mode, set_type)
        )
    
    return constraint_data_objects

def parse_counterexample(result_str: str) -> Dict[str, Tuple[float, float]]:
    """
    Parse the counterexample from the dReal result string.
    
    Args:
        result_str: String representation of dReal result box
        
    Returns:
        Dictionary mapping variable names to (min, max) value ranges
    """
    counterexample = {}
    try:
        for line in result_str.strip().split('\n'):
            # Parse each variable and its range
            if ':' not in line:
                continue
                
            variable, value_range = line.split(':')
            value_range = value_range.strip()
            
            if value_range.startswith('[') and value_range.endswith(']'):
                # Extract lower and upper bounds
                bounds = value_range.strip('[] ').split(',')
                if len(bounds) == 2:
                    lower, upper = map(float, bounds)
                    counterexample[variable.strip()] = (lower, upper)
                    
    except Exception as e:
        logger.error(f"Failed to parse counterexample: {e}")
        return None
        
    return counterexample