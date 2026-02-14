"""OpenZL Training Hyperparameter Configuration — evolved by OpenEvolve.

This module defines a single function, get_training_config(), that returns
a dictionary of hyperparameters controlling the OpenZL training pipeline.

OpenEvolve will mutate the function body inside the EVOLVE-BLOCK markers
to search for configurations that maximize compression ratio on genomic data.

=== Hyperparameter Reference ===

trainer:           Training algorithm.
                   "greedy"     — default, thorough but slowest
                   "full-split" — fastest, tries every codec on each stream
                   "bottom-up"  — middle ground, merges similar streams

max_time_secs:     Wall-clock training budget (seconds). More time lets the
                   trainer explore more of the compression plan DAG, but with
                   diminishing returns. Range: 60–1800.

no_ace_successors: If True, skip ACE (Asymmetric Context Encoding) successor
                   models. ACE can boost ratio on in-distribution data but
                   may hurt generalization to unseen genome regions.

no_clustering:     If True, skip the clustering phase of training. Clustering
                   groups similar data streams together. Disabling it can
                   speed up training but may reduce compression ratio.

target_train_mib:  Size of the training sample in MiB. Must be one of
                   {50, 100, 200} to match the pre-prepared datasets.
                   Larger samples give the trainer more representative data
                   but take longer to process.

threads:           Number of CPU threads for parallel training operations.
                   More threads = faster training, but with diminishing
                   returns after the number of physical cores.

compress_jobs:     Number of parallel chunk compression jobs during evaluation.
                   Only affects evaluation speed, not compression quality.
"""


# EVOLVE-BLOCK-START
def get_training_config():
    """Return OpenZL training hyperparameter configuration.

    Each key maps to a zli train flag or pipeline parameter.
    The evaluator calls this function and uses the returned dict.
    """
    return {
        "trainer": "greedy",        # "greedy" | "full-split" | "bottom-up"
        "max_time_secs": 1800,      # training time budget (seconds)
        "no_ace_successors": True,  # True = disable ACE, False = enable ACE
        "no_clustering": False,     # True = skip clustering
        "target_train_mib": 200,    # 50, 100, or 200 (must match prepared data)
        "threads": 16,              # training thread count
        "compress_jobs": 4,         # parallel compression jobs
    }
# EVOLVE-BLOCK-END
