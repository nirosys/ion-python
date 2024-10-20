# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import gc
import os
import platform
import tempfile
import time
import timeit

from amazon.ionbenchmark.benchmark_spec import BenchmarkSpec
import amazon.ionbenchmark.Format as _format
from amazon.ionbenchmark.sample_dist import SampleDist

_pypy = platform.python_implementation() == 'PyPy'
if not _pypy:
    import tracemalloc


class BenchmarkResult:
    """
    Results generated by the `run_benchmark` function.
    """
    nanos_per_op: SampleDist = None
    ops_per_second: SampleDist = None
    peak_memory_usage = None  # measured in bytes

    def __init__(self, nanos_per_op, ops_per_second, peak_memory_usage):
        self.nanos_per_op = SampleDist(nanos_per_op)
        self.ops_per_second = SampleDist(ops_per_second)
        self.peak_memory_usage = peak_memory_usage


def run_benchmark(benchmark_spec: BenchmarkSpec):
    """
    Run benchmarks for `benchmark_spec`.

    The overall approach of this runner is to time multiple samples where each sample consists of multiple invocations
    of the test function. As a rule of thumb, a sample size of 30 is the minimum needed to have a useful level of
    confidence in the results. The margin of error is (roughly speaking) inversely proportional to the square root of
    the sample size, so adding more samples can increase the confidence, but it will have increasingly diminishing
    improvements. As a rule of thumb, it's never worth having a sample size greater than 1000.

    This approach is sound because of the Central Limit Theorem. For an approachable introduction, see
    https://www.kristakingmath.com/blog/sampling-distribution-of-the-sample-mean.

    The reason for multiple invocations per sample is to prevent very short functions from being dominated by
    differences in memory locations or other small differences from one sample to the next. This runner uses the `Timer`
    utility's `autorange()` function to determine the number of times the function must be invoked for it to run for
    at least 1 second. That number is then used as the number of invocations for _every_ sample in the set.
    """
    test_fun = _create_test_fun(benchmark_spec)

    # memory profiling
    if _pypy:
        peak_memory_usage = None
    else:
        peak_memory_usage = _trace_memory_allocation(test_fun)

    setup = ""
    if benchmark_spec["py_gc_disabled"]:
        setup += "import gc; gc.disable()"
    else:
        setup += "import gc; gc.enable()"

    timer = timeit.Timer(stmt=test_fun, timer=time.perf_counter_ns, setup=setup)

    # warm up
    timer.timeit(benchmark_spec.get_warmups())

    # TODO: Consider making the target batch time or the batch size configurable instead of using this hack.
    if "PYTEST_CURRENT_TEST" in os.environ:
        # make the unit tests run in a reasonable time
        batch_size = 1
    else:
        # range-finding
        # This needs the default timer (measuring in seconds) to work correctly, so it's a different Timer instance.
        (batch_size, _) = timeit.Timer(stmt=test_fun, setup=setup).autorange()
        # Ad hoc testing indicates that samples of 1-2 seconds give tighter results than the default 0.2 seconds, but for
        # very quick testing, this can be annoyingly slow.
        batch_size *= 5  # ~1-2 seconds

    # sample collection (iterations)
    raw_timings = timer.repeat(benchmark_spec.get_iterations(), batch_size)

    # Normalize the samples (i.e. remove the effect of the batch size) before returning the results
    nanos_per_op = [t/batch_size for t in raw_timings]
    ops_per_sec = [1000000000.0 / t for t in nanos_per_op]

    return BenchmarkResult(nanos_per_op, ops_per_sec, peak_memory_usage)


def _create_test_fun(benchmark_spec: BenchmarkSpec):
    """
    Create a benchmark function for the given `benchmark_spec`.
    """
    loader_dumper = benchmark_spec.get_loader_dumper()
    match_arg = [benchmark_spec.get_io_type(), benchmark_spec.get_command(), benchmark_spec.get_api()]

    if match_arg == ['buffer', 'read', 'load_dump']:
        with open(benchmark_spec.get_input_file(), 'rb') as f:
            buffer = f.read()

        def test_fn():
            return loader_dumper.loads(buffer)

    elif match_arg == ['buffer', 'write', 'load_dump']:
        data_obj = benchmark_spec.get_data_object()

        def test_fn():
            return loader_dumper.dumps(data_obj)

    elif match_arg == ['file', 'read', 'load_dump']:
        data_file = benchmark_spec.get_input_file()

        def test_fn():
            with open(data_file, "rb") as f:
                return loader_dumper.load(f)

    elif match_arg == ['file', 'write', 'load_dump']:
        data_obj = benchmark_spec.get_data_object()
        data_format = benchmark_spec.get_format()
        if _format.format_is_binary(data_format) or _format.format_is_ion(data_format):
            def test_fn():
                with tempfile.TemporaryFile(mode="wb") as f:
                    return loader_dumper.dump(data_obj, f)
        else:
            def test_fn():
                with tempfile.TemporaryFile(mode="wt") as f:
                    return loader_dumper.dump(data_obj, f)

    else:
        raise NotImplementedError(f"Argument combination not supported: {match_arg}")

    return test_fn


def _trace_memory_allocation(test_fn, *args, **kwargs):
    """
    Measure the memory allocations in bytes for a single invocation of test_fn
    """
    gc.disable()
    tracemalloc.start()
    test_fn(*args, **kwargs)
    memory_usage_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    gc.enable()
    return memory_usage_peak
