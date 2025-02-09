import time
import logging
from typing import Dict, Any, Tuple, Optional, Callable
from certreach.verification.symbolic import extract_symbolic_model
from certreach.verification.dreal_utils import (
    extract_dreal_partials,
    process_dreal_result
)

logger = logging.getLogger(__name__)

def verify_system(
    model, 
    root_path: str, 
    system_type: str, 
    epsilon: float,
    verification_fn: Callable,
    symbolic_model: Optional[Any] = None
) -> Tuple[Dict[str, Any], Dict[str, float], Any]:
    """
    Verify a trained model using dReal with option to reuse symbolic model.
    
    Args:
        model: The trained PyTorch model (already on CPU)
        root_path: Path to save verification results
        system_type: Type of system to verify ('double_integrator', 'triple_integrator', 'three_state')
        epsilon: Verification tolerance
        verification_fn: System-specific verification function
        symbolic_model: Optional precomputed symbolic model
        
    Returns:
        Tuple[dict, dict, Any]: (Verification results, Timing information, Symbolic model)
    """
    timing_info = {}
    logger.info(f"Starting {system_type} verification")
    
    logger.info("Extracting symbolic model")
    try:
        # Time symbolic model generation
        if symbolic_model is None:
            t_symbolic_start = time.time()
            symbolic_model = extract_symbolic_model(model, root_path)
            timing_info['symbolic_time'] = time.time() - t_symbolic_start
        else:
            timing_info['symbolic_time'] = 0.0

        # Time dReal verification setup and execution
        t_verify_start = time.time()
        in_features = 3 if system_type == 'double_integrator' else 4
        result = extract_dreal_partials(symbolic_model, in_features=in_features)
        
        logger.info("Running dReal verification")
        verification_result = verification_fn(
            dreal_partials=result["dreal_partials"],
            dreal_variables=result["dreal_variables"],
            epsilon=epsilon,
            reachMode='forward',
            reachAim='reach',
            setType='set',
            save_directory=root_path
        )
        timing_info['verification_time'] = time.time() - t_verify_start
        
        logger.info("Processing verification results")
        logger.info(f"Symbolic generation took: {timing_info.get('symbolic_time', 0):.2f}s")
        logger.info(f"Verification took: {timing_info['verification_time']:.2f}s")
        
        return process_dreal_result(f"{root_path}/dreal_result.json"), timing_info, symbolic_model
        
    except Exception as e:
        logger.error(f"Verification failed: {str(e)}")
        raise
