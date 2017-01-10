#!/usr/bin/env python3

from os import listdir, makedirs
from os.path import abspath, dirname, join
import argparse
from multiprocessing import Process, SimpleQueue, Manager, Event
import time
import sys
import itertools

# Add the source directory to the path to ensure the imports work
src_path = join(dirname(abspath(__file__)), "../../src")
sys.path.insert(0, src_path)

# Local project imports
import dataflow
import tac_cfg
import logger
ll = logger.log_low


# Indices for different contract analysis categories.
UNRESOLVED = 0
RESOLVED = 1
TIMEOUT = 3
ERROR = 4

DEFAULT_CONTRACT_DIR = '../../../contract_dump/contracts'
"""Directory to fetch contract files from by default."""

DEFAULT_RESULTS_DIR = 'results/'
"""Directory to write results to by default."""

DEFAULT_TIMEOUT = 120
"""Default time before killing analysis of a contract."""

DEFAULT_MAX_ITER = -1
"""Default max analysis iteration count."""

DEFAULT_BAILOUT = -1
"""Default analysis bailout time in seconds."""

FLUSH_PERIOD = 3
"""Wait a little to flush the files and join the processes when concluding."""


parser = argparse.ArgumentParser(
  description="A batch analyser for EVM bytecode programs.")

parser.add_argument("-c",
                    "--contract_dir",
                    nargs="?",
                    default=DEFAULT_CONTRACT_DIR,
                    const=DEFAULT_CONTRACT_DIR,
                    metavar="DIR",
                    help="the location to grab contracts from (as bytecode "
                         "files).")

parser.add_argument("-r",
                    "--results_dir",
                    nargs="?",
                    default=DEFAULT_RESULTS_DIR,
                    const=DEFAULT_RESULTS_DIR,
                    metavar="DIR",
                    help="the location to write the results.")

parser.add_argument("-f",
                    "--from_file",
                    nargs="?",
                    default=None,
                    metavar="FILE",
                    help="A file to extract the filenames of the contracts "
                         "to analyse from, rather than simply processing all "
                         "files in the contracts directory.")

parser.add_argument("-n",
                    "--num_contracts",
                    type=int,
                    nargs="?",
                    default=None,
                    metavar="NUM",
                    help="The maximum number of contracts to process in this "
                         "batch. Unlimited by default.")

parser.add_argument("-k",
                    "--skip",
                    type=int,
                    nargs="?",
                    default=0,
                    const=0,
                    metavar="NUM",
                    help="Skip the the analysis of the first NUM contracts.")

parser.add_argument("-t",
                    "--timeout_secs",
                    type=int,
                    nargs="?",
                    default=DEFAULT_TIMEOUT,
                    const=DEFAULT_TIMEOUT,
                    metavar="SECONDS",
                    help="Forcibly halt analysing any single contact after "
                         "the specified number of seconds.")

parser.add_argument("-I",
                    "--max_iter",
                    type=int,
                    nargs="?",
                    default=DEFAULT_MAX_ITER,
                    const=DEFAULT_MAX_ITER,
                    metavar="ITERATIONS",
                    help="perform no more than the specified number of "
                         "analysis iterations. Lower is faster, but "
                         "potentially less precise. A negative value specifies "
                         "no cap on the iteration count. No cap by default.")

parser.add_argument("-T",
                    "--bail_time",
                    type=int,
                    nargs="?",
                    default=DEFAULT_BAILOUT,
                    const=DEFAULT_BAILOUT,
                    metavar="SECONDS",
                    help="begin to terminate the analysis if it's looking to "
                         "take more time than the specified number of seconds. "
                         "Bailing out early may mean the analysis is not able "
                         "to reach a fixed-point, so results may be less "
                         "precise. A negative value means no cap on the "
                         "running time. No cap by default.")

parser.add_argument("-q",
                    "--quiet",
                    action="store_true",
                    default=False,
                    help="Silence output.")

parser.add_argument("-s",
                    "--strict",
                    action="store_true",
                    default=False,
                    help="unrecognised opcodes will not be skipped, but will "
                         "result in an error.")


def analyse_contract(filename, result_queue):
  """Perform dataflow analysis on a contract, storing the result in the queue."""
  try:
    with open(join(args.contract_dir, filename)) as file:
      cfg = tac_cfg.TACGraph.from_bytecode(file, strict=args.strict)

      dataflow.analyse_graph(cfg, max_iterations=args.max_iter,
                                  bailout_seconds=args.bail_time)

      if cfg.has_unresolved_jump:
        result_queue.put((filename, UNRESOLVED))
        ll("Unresolved.")
      else:
        result_queue.put((filename, RESOLVED))

  except Exception as e:
    ll("Error: {}".format(e))
    result_queue.put((filename, ERROR))


def flush_queue(period, run_signal, result_queue, result_dict):
  """For flushing the queue periodically to a dict so it doesn't fill up."""
  while run_signal.is_set():
    time.sleep(period)
    while not result_queue.empty():
      item = result_queue.get()
      result_dict[item[1]] = result_dict[item[1]] + [item[0]]
      # NB: += doesn't work here


args = parser.parse_args()

if args.quiet:
  logger.LOG_LEVEL = logger.Verbosity.SILENT
else:
  logger.LOG_LEVEL = logger.Verbosity.MEDIUM

# Extract contract filenames.
runtime_files = filter(lambda filename: filename.endswith("runtime.hex"),
                       listdir(args.contract_dir))
if args.from_file:
  with open(args.from_file, 'r') as f:
    runtime_files = [l.strip() for l in f.readlines()]

stop_index = None if args.num_contracts is None else args.skip + args.num_contracts
to_process = itertools.islice(runtime_files, args.skip, stop_index)


# Set up multiprocessing stuff.
manager = Manager()
res_dict = manager.dict()
res_queue = SimpleQueue()

res_dict[RESOLVED] = []
res_dict[UNRESOLVED] = []
res_dict[TIMEOUT] = []
res_dict[ERROR] = []

run = Event()
run.set()
flush_proc = Process(target=flush_queue, args=(FLUSH_PERIOD, run,
                                               res_queue, res_dict))
flush_proc.start()

try:
  # Process each contract in turn, timing out if it takes too long.
  for i, fname in enumerate(to_process):
    ll("{}: {}.".format(i, fname))
    proc = Process(target=analyse_contract, args=(fname, res_queue))

    start_time = time.time()
    proc.start()

    while time.time() - start_time < args.timeout_secs:
      if proc.is_alive():
        time.sleep(0.01)
      else:
        proc.join()
        break
    else:
      res_queue.put((fname, TIMEOUT))
      proc.terminate()
      ll("Timed out.")

  # Conclude and write results to file.
  ll("\nFinishing...\n")
  run.clear()
  flush_proc.join(FLUSH_PERIOD + 1)

  makedirs(args.results_dir, exist_ok=True)

  r = res_dict[RESOLVED]
  u = res_dict[UNRESOLVED]
  t = res_dict[TIMEOUT]
  e = res_dict[ERROR]
  total = sum(len(l) for l in res_dict.values())

  with open(join(args.results_dir, "resolved.txt"), 'w') as f:
    f.write("\n".join(r) + "\n")
  with open(join(args.results_dir, "unresolved.txt"), 'w') as f:
    f.write("\n".join(u) + "\n")
  with open(join(args.results_dir, "timeout.txt"), 'w') as f:
    f.write("\n".join(t) + "\n")
  with open(join(args.results_dir, "error.txt"), 'w') as f:
    f.writelines("\n".join(e) + "\n")

  ll("Resolved: {}/{}".format(len(r), total))
  ll("Unresolved: {}/{}".format(len(u), total))
  ll("Timed Out: {}/{}".format(len(t), total))
  ll("Errors: {}/{}".format(len(e), total))
except Exception as e:
  import traceback
  traceback.print_exc()
  flush_proc.terminate()


