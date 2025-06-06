import logging
from typing import Dict, List, Any, Tuple, Optional, Callable
import ast
import time
import multiprocessing as mp
from care.verification.verifier_utils.dreal_utils import dreal_function_map, check_with_dreal, parse_dreal_expression
from care.verification.verifier_utils.marabou_utils import marabou_AVAILABLE
try:
    import z3
    Z3_AVAILABLE = True
except ImportError:
    Z3_AVAILABLE = False
if marabou_AVAILABLE:
    from care.verification.verifier_utils.marabou_utils import check_with_marabou
if Z3_AVAILABLE:
    from care.verification.verifier_utils.z3_utils import z3_function_map, check_with_z3, parse_z3_expression, Z3_AVAILABLE

function_maps = {
    'z3': z3_function_map if Z3_AVAILABLE else None,
    'dreal': dreal_function_map,
    'marabou': dreal_function_map if marabou_AVAILABLE else None  # Marabou uses dReal function map
}

logger = logging.getLogger(__name__)

def rebuild_constraint(func_map,
                    constraint_type: str, 
                    variables: Dict[str, Any],
                    value_fn_expr: str,
                    partials_expr: Dict[str, str],
                    boundary_fn_expr: str,
                    hamiltonian_expr: Callable,
                    epsilon: float,
                    is_initial_time: bool = False):
    """
    Rebuild a dReal/z3 constraint from its components.
    
    Args:
        constraint_type: Type of constraint ('boundary_1', 'derivative_1', etc.)
        variables: Dictionary of dReal/z3 Variables
        value_fn_expr: String representation of value function
        partials_expr: Dictionary of partial derivative strings
        boundary_fn_expr: String representation of boundary function
        hamiltonian_expr: String representation of the Hamiltonian
        epsilon: Verification tolerance
        is_initial_time: Whether this is an initial time constraint
        
    Returns:
        Rebuilt dReal Formula constraint
    """
    # Extract time and state variables
    state_vars = []
    partial_state_vars = []
    for key, value in variables.items():
        if key.endswith("_1"):
            continue
        if key.startswith("x_1_"):
            state_vars.append(value)
        elif key.startswith("partial_x_1_"):
            partial_state_vars.append(value)

    t = variables.get('x_1_1')
    dv_dt = variables.get('partial_x_1_1')

    # Parse expressions
    if func_map['solver_name'] == 'z3':
        parse = parse_z3_expression
    elif func_map['solver_name'] == 'dreal':
        parse = lambda s: parse_dreal_expression(s, variables, func_map)
    else:
        raise ValueError(f"Unknown solver: {func_map['solver_name']}")

    value_fn = parse(value_fn_expr)
    boundary_value = parse(boundary_fn_expr)
    hamiltonian_value = parse(hamiltonian_expr)
    
    # Add partial constraints
    if 'partial' in hamiltonian_expr:
        partial_constraints = [func_map['and'](variables[key] == parse(expr)) for key, expr in partials_expr.items()]
    else:
        partial_constraints = []
        dv_dt = parse(partials_expr['partial_x_1_1'])
    
    # Define state constraints
    if is_initial_time:
        time_constraint = (t == 0)
    else:
        time_constraint = func_map['and'](t >= 0, t <= 1)
        
    space_constraints = [func_map['and'](var >= -1, var <= 1) for var in state_vars]
    
    # Build the specified constraint
    if constraint_type == 'derivative_boundary':
        derivative_constraint = func_map['abs'](dv_dt + hamiltonian_value) > epsilon
        boundary_constraint = func_map['and'](func_map['abs'](value_fn - boundary_value) > epsilon, (t == 0))
        return func_map['and'](time_constraint, *space_constraints, func_map['or'](derivative_constraint,boundary_constraint) , *partial_constraints)
    elif constraint_type == 'boundary':
        return func_map['and'](time_constraint, *space_constraints, func_map['abs'](value_fn - boundary_value) > epsilon)
    elif constraint_type == 'derivative':
        derivative_constraint = func_map['abs'](dv_dt + hamiltonian_value) > epsilon
        return func_map['and'](time_constraint, *space_constraints, derivative_constraint, *partial_constraints)
    # Handle split cases
    elif constraint_type == 'boundary_1':
        return func_map['and'](time_constraint, *space_constraints, value_fn - boundary_value > epsilon)
    elif constraint_type == 'boundary_2':
        return func_map['and'](time_constraint, *space_constraints, value_fn - boundary_value < -epsilon)
    elif constraint_type == 'derivative_1':
        return func_map['and'](time_constraint, *space_constraints, dv_dt + hamiltonian_value < -epsilon, *partial_constraints)
    elif constraint_type == 'derivative_2':
        return func_map['and'](time_constraint, *space_constraints, dv_dt + hamiltonian_value > epsilon, *partial_constraints)
    elif constraint_type == 'target_1':
        return func_map['and'](time_constraint, *space_constraints, 
                    func_map['and'](dv_dt + hamiltonian_value < -epsilon, value_fn - boundary_value < -epsilon), *partial_constraints)
    elif constraint_type == 'target_2':
        return func_map['and'](time_constraint, *space_constraints, dv_dt + hamiltonian_value > epsilon, *partial_constraints)
    elif constraint_type == 'target_3':
        return func_map['and'](time_constraint, *space_constraints, value_fn - boundary_value > epsilon, *partial_constraints)
    else:
        logger.error(f"Unknown constraint type: {constraint_type}")
        return None

def process_check_advanced(solver_name, constraint_data, hamiltonian_expr, value_fn_expr, boundary_fn_expr, partials_expr) -> Tuple[int, Optional[str]]:
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
    # Create the function map based on the solver name
    func_map = function_maps[solver_name]
    if func_map is None:
        logger.error(f"Solver {solver_name} is not available.")
        return constraint_data['constraint_id'], f"Error: Solver {solver_name} is not available."

    # Extract basic constraint data
    constraint_id = constraint_data['constraint_id']
    constraint_type = constraint_data['constraint_type']
    epsilon = constraint_data['epsilon']
    delta = constraint_data['delta']
    is_initial_time = constraint_data['is_initial_time']
    
    # Create variables
    time_var = func_map['variable']("x_1_1")
    state_vars = [func_map['variable'](f"x_1_{i+2}") for i in range(len(constraint_data['space_constraints']))]
    partial_vars = [func_map['variable'](str(key)) for key, _ in partials_expr.items()]
    
    # Create dictionary of all variables
    variables = {"x_1_1": time_var}
    for i, var in enumerate(state_vars):
        variables[str(var)] = var
    for i, var in enumerate(partial_vars):
        variables[str(var)] = var
    # Check constraint
    proc_name = mp.current_process().name
    logger.debug(f"Process {proc_name} checking constraint {constraint_id}: {constraint_type}")
    
    start_time = time.monotonic()
    if solver_name == 'marabou' and constraint_type not in ['boundary_1', 'boundary_2', 'target_1', 'target_3']:
        result = check_with_marabou(constraint_data, 
                            partials_expr,
                            hamiltonian_expr)
    else:
        # Use rebuild_constraint to create the constraint
        constraint = rebuild_constraint(
            func_map=func_map,
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
        
        # Execute the constraint check
        if solver_name == 'z3':
            result = check_with_z3(constraint)
        elif solver_name == 'dreal' or 'marabou':
            result = check_with_dreal(constraint, delta)
        else:
            raise ValueError(f"Unknown solver: {solver_name}")

    check_time = time.monotonic() - start_time
    
    if result:
        logger.info(f"Process {proc_name} found counterexample for constraint {constraint_id} in {check_time:.4f}s")
        return constraint_id, str(result)
    else:
        logger.info(f"Process {proc_name} found no counterexample for constraint {constraint_id} in {check_time:.4f}s")
        return constraint_id, None

def serialize_expression(expr, solver) -> str:
    """
    Convert a dReal/z3 expression to a serializable string format.
    
    Args:
        expr: dReal/z3 expression
        
    Returns:
        String representation of the expression
    """
    if solver == 'z3':
        return expr.serialize()
    else: # For dreal and Marabou, just convert to string
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
                                epsilon_ratio: float,
                                delta: float,
                                min_with: str = 'none',
                                reach_mode: str = 'forward',
                                set_type: str = 'set',
                                time_subdivisions: int = 1) -> List[Dict]:
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
            create_constraint_data(constraint_id, 'boundary_2', True, state_dim, epsilon*epsilon_ratio, delta, 
                                  reach_mode, set_type)
        )
    else:
        # For non-initial time constraints: derivative_1, derivative_2
        for time_range in time_ranges:
            constraint_data_objects.append(
                create_constraint_data(constraint_id, 'derivative_1', False, state_dim, epsilon*(1-epsilon_ratio), delta, 
                                      reach_mode, set_type, time_range)
            )
            constraint_id += 1
            
            constraint_data_objects.append(
                create_constraint_data(constraint_id, 'derivative_2', False, state_dim, epsilon*(1-epsilon_ratio), delta, 
                                      reach_mode, set_type, time_range)
            )
            constraint_id += 1
            
        # Initial time constraints: boundary_1, boundary_2
        constraint_data_objects.append(
            create_constraint_data(constraint_id, 'boundary_1', True, state_dim, epsilon*epsilon_ratio, delta, 
                                  reach_mode, set_type)
        )
        constraint_id += 1
        
        constraint_data_objects.append(
            create_constraint_data(constraint_id, 'boundary_2', True, state_dim, epsilon*epsilon_ratio, delta, 
                                  reach_mode, set_type)
        )
    
    return constraint_data_objects

def parse_counterexample(result_str: str) -> Dict[str, Tuple[float, float]]:
    """
    Parse the counterexample from the dReal/z3 result string.
    
    Args:
        result_str: String representation of dReal/z3 result box
        
    Returns:
        Dictionary mapping variable names to (min, max) value ranges
    """
    counterexample = {}
    try:
        # Check if parsing dict format
        if ':' in result_str:
            if '\n' in result_str:
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
            else:
                result = ast.literal_eval(result_str)
                for key, value in result.items():
                    if isinstance(value, list):
                        counterexample[key] = tuple(value)
                    else:
                        counterexample[key] = (value, value)
        else:
            clean_str = result_str.strip('[]')
            # Extract all floating point numbers
            counterexample = [float(x) for x in clean_str.split(',')]
    except Exception as e:
        logger.error(f"Failed to parse counterexample: {e}")
        return None
        
    return counterexample