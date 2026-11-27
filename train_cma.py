#!/usr/bin/env python3
'''CMA-ES search over driver parameters (spec 0010).

Each candidate parameter set is scored by fitness.py (spec 0009, lower =
better) on a list of tracks. Up to 10 candidates run in parallel, one per
scr_server UDP port 3001..3010.

    python3 train_cma.py --outdir cma_runs/run1 [--popsize 20] [--workers 10]
    python3 train_cma.py --outdir cma_runs/run1 --resume

Only the parameters listed in the search space file are optimized; all other
parameters are fixed to the values of --base. The search runs in the
normalized cube [0,1]^n (CMA bounds [0,1]); integer parameters are rounded
when a candidate is written.

Gen 0 of a new run evaluates the base params on every worker port at once;
if the fitness differs between ports by more than --port-check-tol (the
scr_server slots are not identical, e.g. another car) the run exits with
code 3 before CMA starts (spec 0011).

Outputs in --outdir:
    run_config.json         arguments, search space, base params
    generations.csv         one row per generation (gen 0 = base params)
    candidates.jsonl        one line per evaluated candidate
    port_check.json         base params evaluated on every port (spec 0011)
    best.json               best full params dict (usable with snakeoil3_gym.py --params)
    best_result.json        fitness.py result JSON of best.json
    checkpoint.pkl          CMA state, written after every generation (--resume)
    failed/                 fitness.py logs of failed evaluations
    work/                   per-port scratch files

Needs numpy and cma (pycma); everything else is the standard library.
--dry-run works without them.
'''

import argparse
import csv
import json
import math
import os
import pickle
import queue
import random
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

HERE= os.path.dirname(os.path.abspath(__file__))

# == Defaults ==
BASE_PORT= 3001           # scr_server driver idx N listens on BASE_PORT + N.
MAX_WORKERS= 10           # scr_server supports driver idx 0..9 -> 10 ports per node.
DEFAULT_SIGMA0= 0.2       # initial step size in the normalized [0,1] space.
DEFAULT_POPSIZE= 20       # candidates per generation (2 waves of 10 workers).
DEFAULT_FAIL_VALUE= 1e6   # fitness of a failed evaluation (fitness.py max is 8 tracks * 10000).
EVAL_TIMEOUT_PER_TRACK= 150.0  # s wall per track for the auto --eval-timeout (fitness.py: 120 s + stop).
EVAL_TIMEOUT_EXTRA= 60.0  # s added to the auto --eval-timeout.
KILL_GRACE_S= 10.0        # s between SIGTERM and SIGKILL for fitness.py (it stops torcs itself).
POLL_S= 0.2               # s between polls of a running evaluation.
MAX_FAILED_LOGS= 200      # keep at most this many logs of failed evaluations.
GEN_TIME_MARGIN= 1.2      # --max-hours: next generation must fit in remaining * 1/margin.
PORT_CHECK_TOL= 1.0       # max fitness spread of the base params across ports (TORCS is deterministic).
PORT_CHECK_EXIT= 3        # exit code when the port check fails.
CHECKPOINT_VERSION= 1

POSIX= os.name == 'posix'
FITNESS_RE= re.compile(r'^FITNESS\s+(\S+)\s*$', re.M)


# ---------------------------------------------------------------- utils

def import_driver():
    '''snakeoil3_gym (stdlib only): DEFAULT_PARAMS and load_params.'''
    sys.path.insert(0, HERE)
    try:
        import snakeoil3_gym
    finally:
        sys.path.pop(0)
    return snakeoil3_gym


def write_json(path, data):
    tmp= path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def write_pickle(path, data):
    tmp= path + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def log(msg):
    print('[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    sys.stdout.flush()


# ---------------------------------------------------------------- search space

class SearchSpace:
    '''Box of optimized parameters. x in [0,1]^n <-> parameter values.
    A parameter is an integer if its DEFAULT_PARAMS value is an int.'''

    def __init__(self, bounds, defaults):
        self.names= list(bounds)
        self.lo= [float(bounds[k][0]) for k in self.names]
        self.hi= [float(bounds[k][1]) for k in self.names]
        self.is_int= [isinstance(defaults[k], int) and not isinstance(defaults[k], bool) for k in self.names]

    @classmethod
    def load(cls, path, defaults):
        '''JSON object {param: [low, high], ...}; keys starting with "_" are comments.'''
        try:
            with open(path, encoding='utf-8') as f:
                data= json.load(f)
        except (OSError, ValueError) as why:
            raise ValueError('cannot read search space %s: %s' % (path, why))
        if not isinstance(data, dict):
            raise ValueError('search space %s must be a JSON object' % path)
        bounds= {}
        for k, v in data.items():
            if k.startswith('_'):
                continue
            if k not in defaults:
                raise ValueError('search space: unknown parameter %r' % k)
            if (not isinstance(v, list) or len(v) != 2
                    or any(isinstance(b, bool) or not isinstance(b, (int, float)) for b in v)):
                raise ValueError('search space: %s must be [low, high]' % k)
            if not (math.isfinite(v[0]) and math.isfinite(v[1]) and v[0] < v[1]):
                raise ValueError('search space: %s needs finite low < high, got %r' % (k, v))
            bounds[k]= v
        if not bounds:
            raise ValueError('search space %s is empty' % path)
        return cls(bounds, defaults)

    def as_dict(self):
        return {k: [lo, hi] for k, lo, hi in zip(self.names, self.lo, self.hi)}

    def __len__(self):
        return len(self.names)

    def normalize(self, params):
        '''Params dict -> list of floats in [0,1] (clipped).'''
        return [min(1.0, max(0.0, (float(params[k]) - lo) / (hi - lo)))
                for k, lo, hi in zip(self.names, self.lo, self.hi)]

    def out_of_bounds(self, params):
        return [k for k, lo, hi in zip(self.names, self.lo, self.hi) if not lo <= params[k] <= hi]

    def denormalize(self, x):
        '''x (any sequence of n floats) -> {param: value}; clipped to the box,
        integers rounded (and kept inside the box).'''
        out= {}
        for k, lo, hi, is_int, xi in zip(self.names, self.lo, self.hi, self.is_int, x):
            xi= min(1.0, max(0.0, float(xi)))
            v= lo + xi * (hi - lo)
            if is_int:
                v= int(round(v))
                v= max(int(math.ceil(lo)), min(int(math.floor(hi)), v))
            out[k]= v
        return out

    def full_params(self, base, x):
        P= dict(base)
        P.update(self.denormalize(x))
        return P


# ---------------------------------------------------------------- port locks

class PortLocks:
    '''Exclusive per-node locks on scr_server ports (flock on files in
    --lock-dir, /tmp by default), so two jobs on one node do not share a port.
    No-op on Windows.'''

    def __init__(self, lock_dir):
        self.lock_dir= lock_dir
        self.files= {}

    def acquire(self, ports, want):
        got= []
        if not POSIX or not self.lock_dir:
            return list(ports)[:want]
        import fcntl
        os.makedirs(self.lock_dir, exist_ok=True)
        for p in ports:
            if len(got) >= want:
                break
            f= open(os.path.join(self.lock_dir, 'scr_port_%d.lock' % p), 'a')
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                continue
            self.files[p]= f
            got.append(p)
        return got

    def release(self):
        for f in self.files.values():
            try:
                f.close()
            except OSError:
                pass
        self.files= {}


# ---------------------------------------------------------------- evaluation

class Evaluator:
    '''Runs fitness.py for one candidate on a free port. Thread safe; the
    number of concurrent runs equals the number of ports.'''

    def __init__(self, args, outdir, ports, stop_event):
        self.args= args
        self.work= os.path.join(outdir, 'work')
        self.failed_dir= os.path.join(outdir, 'failed')
        os.makedirs(self.work, exist_ok=True)
        self.ports= queue.Queue()
        for p in ports:
            self.ports.put(p)
        self.stop_event= stop_event
        self.failed_logs= 0
        self.lock= threading.Lock()
        n_tracks= len([t for t in args.tracks.split(',') if t.strip()]) if args.tracks else 8
        self.timeout= args.eval_timeout if args.eval_timeout > 0 else (
            n_tracks * args.laps * EVAL_TIMEOUT_PER_TRACK + EVAL_TIMEOUT_EXTRA)

    def command(self, port, params_path, workdir, out):
        cmd= shlex.split(self.args.python) if self.args.python else [sys.executable]
        cmd+= ['-u', self.args.fitness_script, '--params', params_path, '--port', str(port),
               '--workdir', workdir, '--out', out, '--laps', str(self.args.laps)]
        if self.args.tracks:
            cmd+= ['--tracks', self.args.tracks]
        if self.args.fitness_args:
            cmd+= shlex.split(self.args.fitness_args)
        return cmd

    def evaluate(self, tag, params):
        port= self.ports.get()
        try:
            return self._run(port, tag, params)
        finally:
            self.ports.put(port)

    def evaluate_on(self, port, tag, params):
        '''Run on the given port, bypassing the port queue. Only while no
        evaluate() call is running (port check before/after CMA generations).'''
        return self._run(port, tag, params)

    def _terminate(self, p):
        try:
            if POSIX:
                p.send_signal(signal.SIGTERM)  # fitness.py stops torcs/driver in its finally
            else:
                p.terminate()
        except OSError:
            pass
        try:
            p.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                if POSIX:
                    os.killpg(p.pid, signal.SIGKILL)
                else:
                    p.kill()
            except OSError:
                pass
            try:
                p.wait(KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass

    def _run(self, port, tag, params):
        base= os.path.join(self.work, 'p%d' % port)
        params_path= base + '_params.json'
        out= base + '_result.json'
        log_path= base + '.log'
        workdir= base
        write_json(params_path, params)
        if os.path.exists(out):
            os.remove(out)
        res= {'tag': tag, 'port': port, 'fitness': self.args.fail_value, 'status': 'fail',
              'wall_s': 0.0, 'result': None}
        if self.stop_event.is_set():
            res['status']= 'stopped'
            return res
        t0= time.monotonic()
        kw= {'start_new_session': True} if POSIX else {}
        try:
            with open(log_path, 'w') as logf:
                p= subprocess.Popen(self.command(port, params_path, workdir, out), stdout=logf,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=HERE, **kw)
        except OSError as why:
            res['status']= 'cannot start: %s' % why
            return res
        status= None
        while p.poll() is None:
            if self.stop_event.is_set():
                status= 'stopped'
                break
            if time.monotonic() - t0 > self.timeout:
                status= 'timeout'
                break
            time.sleep(POLL_S)
        if status is not None:
            self._terminate(p)
        res['wall_s']= time.monotonic() - t0
        if status is None and p.returncode != 0:
            status= 'exit %d' % p.returncode
        if status is None:
            f, data= self._parse(out, log_path)
            if f is None:
                status= 'no fitness'
            else:
                res.update({'fitness': f, 'status': 'ok', 'result': data})
        if status is not None:
            res['status']= status
            if status != 'stopped':
                self._keep_failed_log(log_path, tag)
        return res

    @staticmethod
    def _parse(out, log_path):
        '''Fitness from the result JSON, else from the last FITNESS line of the log.'''
        data= None
        try:
            with open(out, encoding='utf-8') as f:
                data= json.load(f)
            f_val= float(data['fitness'])
            if math.isfinite(f_val):
                return f_val, data
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            with open(log_path, encoding='utf-8', errors='replace') as f:
                found= FITNESS_RE.findall(f.read())
            if found:
                f_val= float(found[-1])
                if math.isfinite(f_val):
                    return f_val, data
        except (OSError, ValueError):
            pass
        return None, None

    def _keep_failed_log(self, log_path, tag):
        with self.lock:
            if self.failed_logs >= MAX_FAILED_LOGS:
                return
            self.failed_logs+= 1
        try:
            os.makedirs(self.failed_dir, exist_ok=True)
            shutil.copyfile(log_path, os.path.join(self.failed_dir, '%s.log' % tag))
        except OSError:
            pass


def track_summary(result):
    '''Short per-track list from a fitness.py result JSON.'''
    if not result:
        return None
    return [{'track': t.get('track'), 'status': t.get('status'), 'score': t.get('score'),
             'lap_time_total': t.get('lap_time_total'), 'damage': t.get('damage'),
             'offtrack_s': t.get('offtrack_s')} for t in result.get('tracks', [])]


def port_check(ev, pool, ports, params, tag, tol, path):
    '''Evaluate the same params once on every port in parallel (one wave).
    TORCS is deterministic, so all ports must give the same fitness; a spread
    means the scr_server slots differ (e.g. another car on some ports, spec 0011).
    Returns (verdict, results in port order); verdict is one of
    ok, mismatch, some_failed, all_failed, stopped. Writes a summary to path.'''
    futures= [pool.submit(ev.evaluate_on, p, '%s_p%d' % (tag, p), params) for p in ports]
    pending= set(futures)
    while pending:
        done, pending= wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
    results= [f.result() for f in futures]
    for r in results:
        log('port check: port %d %-10s fitness %12.3f (%.1f s)' % (r['port'], r['status'], r['fitness'], r['wall_s']))
    fits= [r['fitness'] for r in results if r['status'] == 'ok']
    spread= max(fits) - min(fits) if fits else None
    if any(r['status'] == 'stopped' for r in results):
        verdict= 'stopped'
    elif not fits:
        verdict= 'all_failed'
    elif len(fits) < len(results):
        verdict= 'some_failed'
    elif spread > tol:
        verdict= 'mismatch'
    else:
        verdict= 'ok'
    write_json(path, {'tag': tag, 'verdict': verdict, 'tol': tol, 'spread': spread,
                      'checked': time.strftime('%Y-%m-%dT%H:%M:%S'),
                      'ports': [{'port': r['port'], 'status': r['status'], 'fitness': r['fitness'],
                                 'wall_s': round(r['wall_s'], 2), 'tracks': track_summary(r['result'])}
                                for r in results]})
    if verdict == 'ok':
        log('port check passed: %d ports, spread %.3f <= tol %g' % (len(results), spread, tol))
    return verdict, results


def port_check_error(verdict, results, tol, path):
    per_port= ', '.join('%d=%s' % (r['port'], '%.3f' % r['fitness'] if r['status'] == 'ok' else r['status'])
                        for r in results)
    if verdict == 'mismatch':
        fits= [r['fitness'] for r in results]
        what= 'the same params give different fitness on different ports (spread %.3f > tol %g)' % (
            max(fits) - min(fits), tol)
    else:
        what= 'evaluation failed on some ports (logs in failed/)'
    log('ERROR: port check failed: %s: %s. Every scr_server slot must use the same car '
        '(scr_server.xml, spec 0011); details in %s. Disable the check: --skip-port-check '
        '(new run) or no --port-check-on-resume (--resume).'
        % (what, per_port, path))


# ---------------------------------------------------------------- run state

class Run:
    '''Output files of one optimization run.'''

    CSV_HEADER= ['gen', 'evals', 'best', 'median', 'worst', 'sigma', 'best_so_far', 'wall_s']

    def __init__(self, outdir):
        self.outdir= outdir
        self.path= lambda name: os.path.join(outdir, name)

    def append_generation(self, row):
        p= self.path('generations.csv')
        new= not os.path.exists(p)
        with open(p, 'a', newline='', encoding='utf-8') as f:
            w= csv.writer(f, lineterminator='\n')
            if new:
                w.writerow(self.CSV_HEADER)
            w.writerow([row['gen'], row['evals']] + ['%.6f' % row[k] for k in
                       ('best', 'median', 'worst', 'sigma', 'best_so_far')] + ['%.1f' % row['wall_s']])

    def append_candidates(self, lines):
        with open(self.path('candidates.jsonl'), 'a', encoding='utf-8', newline='\n') as f:
            for line in lines:
                f.write(json.dumps(line, sort_keys=True) + '\n')

    def write_best(self, params, result):
        write_json(self.path('best.json'), params)
        if result is not None:
            write_json(self.path('best_result.json'), result)


# ---------------------------------------------------------------- main

def build_parser():
    ap= argparse.ArgumentParser(
        description='CMA-ES search over driver parameters; candidates are scored by fitness.py '
                    '(lower = better), up to 10 in parallel on ports 3001..3010.')
    ap.add_argument('--outdir', default=None, help='output directory [cma_runs/<timestamp>]')
    ap.add_argument('--resume', action='store_true', help='continue from <outdir>/checkpoint.pkl')
    ap.add_argument('--base', default=os.path.join(HERE, 'params', 'default.json'),
                    help='params JSON: start point and values of the fixed params [%(default)s]')
    ap.add_argument('--space', default=os.path.join(HERE, 'params', 'search_space.json'),
                    help='search space JSON {param: [low, high]} [%(default)s]')
    ap.add_argument('--popsize', type=int, default=DEFAULT_POPSIZE, help='CMA population size [%(default)s]')
    ap.add_argument('--sigma0', type=float, default=DEFAULT_SIGMA0,
                    help='initial step size in the normalized [0,1] space [%(default)s]')
    ap.add_argument('--seed', type=int, default=None, help='random seed [random]')
    ap.add_argument('--cma-opts', default=None, help='extra pycma options as a JSON object')
    ap.add_argument('--workers', type=int, default=MAX_WORKERS,
                    help='parallel evaluations, one scr_server port each, 1..%d [%%(default)s]' % MAX_WORKERS)
    ap.add_argument('--max-gens', type=int, default=0, help='stop after this generation (0 = no limit)')
    ap.add_argument('--max-hours', type=float, default=0.0,
                    help='stop before the next generation would exceed this wall time of this process (0 = no limit)')
    ap.add_argument('--tracks', default=None, help='passed to fitness.py --tracks [fitness.py default, 8 tracks]')
    ap.add_argument('--laps', type=int, default=1, help='passed to fitness.py --laps [%(default)s]')
    ap.add_argument('--fitness-args', default='',
                    help='extra fitness.py arguments, one string (shlex split), e.g. "--time-limit-factor 2"')
    ap.add_argument('--eval-timeout', type=float, default=0.0,
                    help='s wall per evaluation before it is killed (0 = %g*tracks*laps + %g)'
                    % (EVAL_TIMEOUT_PER_TRACK, EVAL_TIMEOUT_EXTRA))
    ap.add_argument('--fail-value', type=float, default=DEFAULT_FAIL_VALUE,
                    help='fitness of a failed evaluation [%(default)s]')
    ap.add_argument('--lock-dir', default='/tmp',
                    help='node-local dir for port lock files ("" = no locks, Linux only) [%(default)s]')
    ap.add_argument('--python', default=None, help='command to run fitness.py (shlex split) [this interpreter]')
    ap.add_argument('--fitness-script', default=os.path.join(HERE, 'fitness.py'),
                    help='evaluator script, must accept the fitness.py arguments (for tests) [%(default)s]')
    ap.add_argument('--port-check-tol', type=float, default=PORT_CHECK_TOL,
                    help='gen 0 evaluates the base params on every worker port; exit with code 3 if '
                         'max - min fitness > this [%(default)s]')
    ap.add_argument('--skip-port-check', action='store_true',
                    help='new run: evaluate the base params once (no per-port check)')
    ap.add_argument('--port-check-on-resume', action='store_true',
                    help='--resume: run the port check with the base params of the run before continuing')
    ap.add_argument('--dry-run', action='store_true',
                    help='no evaluations: print the search space, write base + 3 random candidates to '
                         '<outdir>/dry_run/ and validate them (no numpy/cma needed)')
    return ap


def print_space(space, base):
    x0= space.normalize(base)
    log('search space: %d params' % len(space))
    for k, lo, hi, is_int, x in zip(space.names, space.lo, space.hi, space.is_int, x0):
        log('  %-20s [%10g, %10g]%s base %-10g x0 %.3f' % (k, lo, hi, ' int' if is_int else '    ', base[k], x))


def dry_run(space, base, outdir, drv):
    d= os.path.join(outdir, 'dry_run')
    os.makedirs(d, exist_ok=True)
    rnd= random.Random(1)
    xs= [('base', space.normalize(base))] + [('rand%d' % i, [rnd.random() for _ in space.names]) for i in range(3)]
    for name, x in xs:
        P= space.full_params(base, x)
        p= os.path.join(d, name + '.json')
        write_json(p, P)
        eff= drv.load_params(p)  # same validation as the driver / fitness.py
        assert eff == P, 'load_params changed the candidate'
        back= space.normalize(P)
        log('%-6s %s' % (name, ' '.join('%s=%g' % (k, P[k]) for k in space.names)))
        log('       x  %s' % ' '.join('%.3f' % v for v in x))
        log('       x\' %s' % ' '.join('%.3f' % v for v in back))
    log('dry run: %d candidate files in %s (validated with snakeoil3_gym.load_params)' % (len(xs), d))


def main(argv=None):
    ap= build_parser()
    args= ap.parse_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        ap.error('--workers must be in 1..%d' % MAX_WORKERS)
    if args.popsize < 2:
        ap.error('--popsize must be >= 2')
    if not args.sigma0 > 0:
        ap.error('--sigma0 must be > 0')
    if args.laps < 1:
        ap.error('--laps must be >= 1')
    if not args.port_check_tol >= 0:
        ap.error('--port-check-tol must be >= 0')
    args.fitness_script= os.path.abspath(args.fitness_script)
    if not os.path.isfile(args.fitness_script):
        ap.error('fitness script not found: %s' % args.fitness_script)

    drv= import_driver()
    try:
        space= SearchSpace.load(args.space, drv.DEFAULT_PARAMS)
        base= drv.load_params(args.base)
    except ValueError as why:
        ap.error(str(why))
    cma_opts_extra= {}
    if args.cma_opts:
        try:
            cma_opts_extra= json.loads(args.cma_opts)
            assert isinstance(cma_opts_extra, dict)
        except (ValueError, AssertionError):
            ap.error('--cma-opts must be a JSON object')

    outdir= os.path.abspath(args.outdir or os.path.join('cma_runs', time.strftime('%Y%m%d-%H%M%S')))
    os.makedirs(outdir, exist_ok=True)
    run= Run(outdir)
    ckpt_path= run.path('checkpoint.pkl')

    if args.dry_run:
        print_space(space, base)
        dry_run(space, base, outdir, drv)
        return 0

    import numpy as np
    import cma

    t_start= time.time()
    stop_event= threading.Event()
    stop_reason= []

    def on_signal(signum, frame):
        if not stop_event.is_set():
            stop_reason.append('signal %d' % signum)
        stop_event.set()
    signal.signal(signal.SIGINT, on_signal)
    if POSIX:
        signal.signal(signal.SIGTERM, on_signal)

    # -- State: new run or resume --
    if args.resume:
        if not os.path.exists(ckpt_path):
            ap.error('--resume: no checkpoint %s' % ckpt_path)
        with open(ckpt_path, 'rb') as f:
            st= pickle.load(f)
        if st.get('version') != CHECKPOINT_VERSION:
            ap.error('checkpoint version %r, expected %d' % (st.get('version'), CHECKPOINT_VERSION))
        if st['space'] != space.as_dict():
            ap.error('search space differs from the checkpoint; use the space of the run:\n%s'
                     % json.dumps(st['space'], sort_keys=True))
        base= st['base']  # fixed params of the run, --base is ignored
        if (st['tracks'], st['laps'], st['fitness_args']) != (args.tracks, args.laps, args.fitness_args):
            log('WARNING: tracks/laps/fitness-args differ from the run (%r, %r, %r); best_so_far is not comparable'
                % (st['tracks'], st['laps'], st['fitness_args']))
        es= st['es']
        np.random.set_state(st['np_random_state'])
        log('resumed %s: gen %d, evals %d, best %.3f, sigma %.4f (--popsize/--sigma0/--seed/--base ignored)'
            % (ckpt_path, st['gen'], st['evals'], st['best_f'], es.sigma))
    else:
        if os.path.exists(ckpt_path):
            ap.error('%s exists: use --resume or another --outdir' % ckpt_path)
        oob= space.out_of_bounds(base)
        if oob:
            log('WARNING: base params outside the search space, clipped for x0: %s' % ', '.join(oob))
        st= None

    print_space(space, base)

    # -- Ports --
    locks= PortLocks(args.lock_dir)
    ports= locks.acquire(range(BASE_PORT, BASE_PORT + MAX_WORKERS), args.workers)
    if not ports:
        log('ERROR: no free scr_server port (all %d..%d locked in %s)'
            % (BASE_PORT, BASE_PORT + MAX_WORKERS - 1, args.lock_dir))
        return 1
    if len(ports) < args.workers:
        log('WARNING: only %d free ports, using %d workers' % (len(ports), len(ports)))
    log('workers %d, ports %s' % (len(ports), ','.join(map(str, ports))))
    ev= Evaluator(args, outdir, ports, stop_event)
    log('eval timeout %.0f s, outdir %s' % (ev.timeout, outdir))

    pool= ThreadPoolExecutor(max_workers=len(ports))
    try:
        if st is None:
            # -- Gen 0: base params as reference --
            write_json(run.path('run_config.json'), {
                'argv': sys.argv, 'args': vars(args), 'space': space.as_dict(), 'base': base,
                'started': time.strftime('%Y-%m-%dT%H:%M:%S')})
            write_json(run.path('base.json'), base)
            if args.skip_port_check or len(ports) == 1:
                log('gen 0: evaluating base params %s%s' % (args.base, ' (port check skipped)'
                    if args.skip_port_check else ' (1 port, no port check)'))
                results= [ev.evaluate('g0000_base', base)]
                verdict= 'ok' if results[0]['status'] == 'ok' else (
                    'stopped' if stop_event.is_set() else 'all_failed')
            else:
                log('gen 0: evaluating base params %s on all %d ports (port check, tol %g)'
                    % (args.base, len(ports), args.port_check_tol))
                verdict, results= port_check(ev, pool, ports, base, 'g0000_base', args.port_check_tol,
                                             run.path('port_check.json'))
            r= results[0]  # first port (3001) = reference, as before
            if verdict != 'ok':
                if stop_event.is_set():
                    log('stopped during gen 0 (%s)' % ', '.join(stop_reason))
                    return 0
                if verdict == 'all_failed':
                    log('ERROR: base evaluation failed (%s), see %s; aborting'
                        % (r['status'], os.path.join(outdir, 'failed')))
                    return 1
                port_check_error(verdict, results, args.port_check_tol, run.path('port_check.json'))
                return PORT_CHECK_EXIT
            f0= r['fitness']
            fits0= [x['fitness'] for x in results]
            wall= time.time() - t_start
            run.append_candidates([{'gen': 0, 'idx': i, 'fitness': x['fitness'], 'status': x['status'],
                                    'wall_s': round(x['wall_s'], 2), 'port': x['port'],
                                    'params': {k: base[k] for k in space.names},
                                    'tracks': track_summary(x['result'])}
                                   for i, x in enumerate(results)])
            run.append_generation({'gen': 0, 'evals': len(results), 'best': min(fits0),
                                   'median': statistics.median(fits0), 'worst': max(fits0),
                                   'sigma': args.sigma0, 'best_so_far': f0, 'wall_s': wall})
            run.write_best(base, r['result'])
            log('gen    0 evals %5d base %.3f (port %d, %.1f s)' % (len(results), f0, r['port'], r['wall_s']))

            opts= {'bounds': [0.0, 1.0], 'popsize': args.popsize, 'verbose': -9,
                   'verb_disp': 0, 'verb_log': 0}
            if args.seed is not None:
                opts['seed']= args.seed
            opts.update(cma_opts_extra)
            es= cma.CMAEvolutionStrategy(space.normalize(base), args.sigma0, opts)
            st= {'version': CHECKPOINT_VERSION, 'gen': 0, 'evals': len(results), 'best_f': f0, 'best_params': base,
                 'base': base, 'base_f': f0, 'space': space.as_dict(), 'wall_used': wall,
                 'tracks': args.tracks, 'laps': args.laps, 'fitness_args': args.fitness_args,
                 'seed': es.opts['seed'],
                 'last_gen_s': max(x['wall_s'] for x in results) * math.ceil(args.popsize / float(len(ports)))}
            st['es']= es
            st['np_random_state']= np.random.get_state()
            write_pickle(ckpt_path, st)
            log('CMA-ES: n=%d popsize=%d sigma0=%g seed=%s' % (len(space), es.popsize, args.sigma0, es.opts['seed']))
        elif args.port_check_on_resume and len(ports) > 1:
            # -- Resume: same check with the base params of the run (nothing written to CSV/JSONL) --
            log('resume: port check with the base params of the run on all %d ports (tol %g)'
                % (len(ports), args.port_check_tol))
            verdict, results= port_check(ev, pool, ports, base, 'resume_g%04d_base' % st['gen'],
                                         args.port_check_tol, run.path('port_check.json'))
            if verdict == 'stopped':
                log('stopped during the port check (%s)' % ', '.join(stop_reason))
                return 0
            if verdict != 'ok':
                port_check_error(verdict, results, args.port_check_tol, run.path('port_check.json'))
                return PORT_CHECK_EXIT
            if abs(results[0]['fitness'] - st['base_f']) > args.port_check_tol:
                log('WARNING: base params now score %.3f, gen 0 of the run scored %.3f; '
                    'fitness of old and new generations is not comparable' % (results[0]['fitness'], st['base_f']))

        wall_before= st['wall_used']
        t_run= time.time()
        while True:
            # -- Stop conditions before a new generation --
            if stop_event.is_set():
                break
            if args.max_gens and st['gen'] >= args.max_gens:
                stop_reason.append('max-gens %d' % args.max_gens)
                break
            cma_stop= es.stop()
            if cma_stop:
                stop_reason.append('cma: %s' % ', '.join('%s=%s' % kv for kv in cma_stop.items()))
                break
            if args.max_hours > 0:
                left= args.max_hours * 3600.0 - (time.time() - t_start)
                if left < GEN_TIME_MARGIN * st['last_gen_s']:
                    stop_reason.append('max-hours %g (%.0f s left, next gen ~%.0f s)'
                                       % (args.max_hours, left, st['last_gen_s']))
                    break

            gen= st['gen'] + 1
            t_gen= time.time()
            X= es.ask()
            cands= [space.full_params(base, x) for x in X]
            futures= [pool.submit(ev.evaluate, 'g%04d_%02d' % (gen, i), P) for i, P in enumerate(cands)]
            pending= set(futures)
            while pending:
                done, pending= wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            if stop_event.is_set():
                log('gen %d discarded (stopping)' % gen)
                break  # unfinished generation is not told to CMA; checkpoint = end of gen-1
            results= [f.result() for f in futures]
            fits= [r['fitness'] for r in results]
            es.tell(X, fits)

            i_best= min(range(len(fits)), key=lambda i: fits[i])
            improved= fits[i_best] < st['best_f']
            if improved:
                st['best_f']= fits[i_best]
                st['best_params']= cands[i_best]
                run.write_best(cands[i_best], results[i_best]['result'])
            n_fail= sum(1 for r in results if r['status'] != 'ok')
            st['gen']= gen
            st['evals']+= len(fits)
            st['last_gen_s']= time.time() - t_gen
            st['wall_used']= wall_before + (time.time() - t_run)
            run.append_candidates([{'gen': gen, 'idx': i, 'fitness': r['fitness'], 'status': r['status'],
                                    'wall_s': round(r['wall_s'], 2), 'port': r['port'],
                                    'params': {k: P[k] for k in space.names},
                                    'tracks': track_summary(r['result'])}
                                   for i, (r, P) in enumerate(zip(results, cands))])
            row= {'gen': gen, 'evals': st['evals'], 'best': min(fits), 'median': statistics.median(fits),
                  'worst': max(fits), 'sigma': es.sigma, 'best_so_far': st['best_f'], 'wall_s': st['wall_used']}
            run.append_generation(row)
            st['es']= es
            st['np_random_state']= np.random.get_state()
            write_pickle(ckpt_path, st)
            log('gen %4d evals %5d best %9.3f median %9.3f worst %9.3f sigma %.4f best_so_far %9.3f%s '
                'fail %d gen %.1f s wall %.0f s'
                % (gen, st['evals'], row['best'], row['median'], row['worst'], es.sigma,
                   st['best_f'], ' *' if improved else '  ', n_fail, st['last_gen_s'], st['wall_used']))
    finally:
        stop_event.set()  # any evaluation still running is terminated by its worker
        pool.shutdown(wait=True)
        locks.release()

    log('stopped: %s' % (', '.join(stop_reason) or 'done'))
    log('gen %d, evals %d, best %.3f (base %.3f), best params %s'
        % (st['gen'], st['evals'], st['best_f'], st['base_f'], run.path('best.json')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
