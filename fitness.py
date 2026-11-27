#!/usr/bin/env python3
'''Fitness evaluator for one driver parameter set (spec 0009).

Runs snakeoil3_gym.py with --params on a list of tracks against TORCS in
results mode (`torcs -r race.xml`, no graphics), parses the telemetry CSV of
every run and prints one scalar fitness (lower = better):

    python3 fitness.py --params params/default.json
    ...
    FITNESS 1234.567890

Offline mode (no simulator): score existing telemetry CSVs.

    python3 fitness.py --score-csv runs/0008_local_corkscrew.csv --laps 1

Standard library only. Exit code 0 even when a track fails (the failure is in
the score); 2 only for usage errors.
'''

import argparse
import csv
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

HERE= os.path.dirname(os.path.abspath(__file__))

# == Track set ==
# Default tracks ("category/name"), all measured with params/default.json in spec 0008.
DEFAULT_TRACKS= [
    'road/corkscrew',
    'road/aalborg',
    'road/alpine-1',
    'road/e-track-2',
    'road/g-track-1',
    'road/forza',
    'road/wheel-1',
    'road/ruudskogen',
]
# Reference 1-lap times, s (spec 0008, params/default.json, HPC, torcs -r, car1-ow1).
# Used only to shorten the sim time limit with --time-limit-factor.
REFERENCE_LAP_TIME= {
    'corkscrew': 104.6, 'aalborg': 97.3, 'alpine-1': 189.1, 'e-track-2': 161.9,
    'g-track-1': 55.9, 'forza': 120.3, 'wheel-1': 117.0, 'ruudskogen': 91.2,
}

# == Scoring (all in seconds of "equivalent lap time", minimized) ==
K_DAMAGE= 0.01            # s per damage point (1000 damage = 10 s, ~10% of a lap).
K_OFFTRACK= 1.0           # s per second spent with |trackPos| > OFFTRACK_TRACKPOS.
UNFINISHED_PENALTY= 1.0   # unfinished: T + UNFINISHED_PENALTY*(1 - progress)*T (+ penalties).
FAIL_SCORE= 10000.0       # per track: no CSV / driver error / wall timeout / sim did not start.
SIM_CRASH_PENALTY= 1.0    # sim_crash (spec 0016): unfinished score + SIM_CRASH_PENALTY*T, so any crash
                          # is worse than any unfinished/finished run and a later crash beats an earlier one.
OFFTRACK_TRACKPOS= 1.0    # |trackPos| above this counts as off track (same as the logger).
FINISH_PROGRESS_MIN= 0.995 # last-lap fallback: progress needed when the final crossing is not logged.

# == Run control ==
STEPS_PER_S= 50           # SCR server: one step = 20 ms of sim time.
DEFAULT_MAX_LAP_TIME= 300.0   # s of sim time per lap before the run is cut (unfinished).
DEFAULT_TIME_LIMIT_FACTOR= 3.0  # per-lap limit = min(max_lap_time, factor*reference); 0 = off.
START_MARGIN_S= 3.0       # s of sim time added to the step limit for the start countdown.
DEFAULT_SIM_START_WAIT= 1.5   # s between starting torcs and starting the driver.
DEFAULT_CONNECT_TIMEOUT= 5.0  # s wall; the driver must connect before this. Must stay below
                              # ~7 s: after that snakeoil3_gym.py runs `pkill torcs` itself.
DEFAULT_WALL_TIMEOUT= 120.0   # s wall per track for the driver process.
KILL_GRACE_S= 3.0         # s to wait after SIGINT/SIGTERM before SIGKILL.
POLL_S= 0.05              # s between driver/simulator status polls.
SIM_EXIT_GRACE_S= 1.0     # s to let the driver exit by itself after the simulator exits; still running
                          # after that (and no ***shutdown*** seen) = sim_crash.
SIM_CRASH_MARKER= 'Segmentation fault'  # printed by the `torcs` shell wrapper when torcs-bin crashes.
MIN_ROWS= 10              # fewer CSV rows than this = no data.
BASE_PORT= 3001           # scr_server driver idx N listens on BASE_PORT + N.
MAX_SCR_IDX= 9            # scr_server supports driver idx 0..9.

CSV_COLUMNS= ['step', 'curLapTime', 'lastLapTime', 'distFromStart', 'distRaced',
              'speedX', 'trackPos', 'damage']


# ---------------------------------------------------------------- CSV analysis

def read_telemetry(path):
    '''Read the logger CSV into a list of dicts of floats (only CSV_COLUMNS).
    Rows that do not parse (e.g. a truncated last line) are skipped.
    Returns None if the file is missing or has no header.'''
    try:
        f= open(path, newline='', encoding='utf-8')
    except OSError:
        return None
    rows= []
    with f:
        reader= csv.DictReader(f)
        if not reader.fieldnames or any(c not in reader.fieldnames for c in CSV_COLUMNS):
            return None
        for r in reader:
            try:
                rows.append({c: float(r[c]) for c in CSV_COLUMNS})
            except (TypeError, ValueError):
                continue
    return rows


def estimate_track_length(rows):
    '''Track length L, m. Exact if the car crossed the start line forward:
    at the crossing distRaced grows by (L - dfs_prev) + dfs_cur, so
    L = dR_cur - dR_prev + dfs_prev - dfs_cur. Otherwise L ~ max(distFromStart)
    (the car starts behind the line, error <= grid offset, ~10-25 m).'''
    for a, b in zip(rows, rows[1:]):
        if a['distFromStart'] - b['distFromStart'] > 100.0 and b['distRaced'] > a['distRaced']:
            L= b['distRaced'] - a['distRaced'] + a['distFromStart'] - b['distFromStart']
            if L > 100.0:
                return L, 'wrap'
    return max(r['distFromStart'] for r in rows), 'max_dfs'


def analyze(rows, laps, server_stopped=False):
    '''Metrics of one run from telemetry rows. `laps` = laps of the race,
    `server_stopped` = the driver saw ***shutdown*** (race ended by TORCS).'''
    m= {'rows': len(rows), 'sim_time_s': len(rows) / float(STEPS_PER_S)}
    lap_times= []
    prev_last= None
    off_steps, off_events, was_off= 0, 0, False
    for r in rows:
        last= r['lastLapTime']
        # Same rule as TelemetryLogger: a lap is done when lastLapTime changes to a new positive value.
        if prev_last is not None and last != prev_last and last > 0:
            lap_times.append(last)
        prev_last= last
        off= abs(r['trackPos']) > OFFTRACK_TRACKPOS
        if off:
            off_steps+= 1
            if not was_off:
                off_events+= 1
        was_off= off
    L, how= estimate_track_length(rows)
    dfs0= rows[0]['distFromStart']
    # Distance from the grid to the finish: run up to the line, then `laps` full laps.
    behind= L - dfs0 if 0.0 <= L - dfs0 < 0.5 * L else 0.0
    need= behind + laps * L
    dist= rows[-1]['distRaced']
    progress= min(1.0, max(0.0, dist / need)) if need > 0 else 0.0
    estimated_last= False
    if (len(lap_times) == laps - 1 and server_stopped and progress >= FINISH_PROGRESS_MIN):
        # TORCS may end the race (and the SCR stream) in the same step the car crosses
        # the line, so the final lastLapTime is never logged: use the running lap clock.
        lap_times.append(rows[-1]['curLapTime'] + 1.0 / STEPS_PER_S)
        estimated_last= True
    m.update({
        'laps_done': min(len(lap_times), laps),
        'lap_times': lap_times[:laps],
        'lap_time_total': sum(lap_times[:laps]),
        'last_lap_estimated': estimated_last,
        'damage': rows[-1]['damage'],
        'offtrack_steps': off_steps,
        'offtrack_s': off_steps / float(STEPS_PER_S),
        'offtrack_events': off_events,
        'dist_raced': dist,
        'max_speed': max(r['speedX'] for r in rows),
        'track_length_est': L,
        'track_length_method': how,
        'dist_needed': need,
        'progress': 1.0 if len(lap_times) >= laps else progress,
    })
    return m


def score_run(m, laps, time_limit_s, failure=None, k_damage=None, k_offtrack=None):
    '''Return (score, status). `m` = analyze() result or None (no data),
    `time_limit_s` = sim time limit of the run (s), `failure` = infra status
    ('timeout', 'driver_error', 'no_connect', 'sim_error', 'sim_crash') or None.
    `k_damage`/`k_offtrack` override K_DAMAGE/K_OFFTRACK when not None.'''
    if failure is not None and not (failure == 'sim_crash' and m is not None):
        return FAIL_SCORE, failure  # sim_crash with data is graded below
    if m is None:
        return FAIL_SCORE, 'no_data'
    kd= K_DAMAGE if k_damage is None else k_damage
    ko= K_OFFTRACK if k_offtrack is None else k_offtrack
    penalty= kd * m['damage'] + ko * m['offtrack_s']
    base= time_limit_s + UNFINISHED_PENALTY * (1.0 - m['progress']) * time_limit_s
    if failure == 'sim_crash':
        return base + SIM_CRASH_PENALTY * time_limit_s + penalty, 'sim_crash'
    if m['laps_done'] >= laps:
        return m['lap_time_total'] + penalty, 'finished'
    return base + penalty, 'unfinished'


def evaluate_csv(path, laps, time_limit_s, server_stopped=False, failure=None,
                 k_damage=None, k_offtrack=None):
    '''Read + analyze + score one CSV. Returns a metrics dict with
    'score' and 'status'.'''
    rows= read_telemetry(path)
    m= analyze(rows, laps, server_stopped) if rows is not None and len(rows) >= MIN_ROWS else None
    score, status= score_run(m, laps, time_limit_s, failure, k_damage, k_offtrack)
    out= dict(m) if m else {}
    out.update({'score': score, 'status': status, 'time_limit_s': time_limit_s})
    return out


# ---------------------------------------------------------------- race config

def make_race_xml(template_text, category, name, laps, driver_idx):
    '''Return race xml text: track name/category in section Tracks/1, laps,
    scr_server driver idx. Plain text substitution (keeps DOCTYPE as is).'''
    m= re.search(r'<section name="Tracks">.*?</section>', template_text, re.S)
    if not m:
        raise ValueError('race template has no Tracks section')
    block= m.group(0)
    block, n1= re.subn(r'(<attstr name="name" val=")[^"]*(")', r'\g<1>%s\g<2>' % name, block, count=1)
    block, n2= re.subn(r'(<attstr name="category" val=")[^"]*(")', r'\g<1>%s\g<2>' % category, block, count=1)
    if n1 != 1 or n2 != 1:
        raise ValueError('race template: track name/category not found')
    text= template_text[:m.start()] + block + template_text[m.end():]
    text, n3= re.subn(r'(<attnum name="laps" val=")[^"]*(")', r'\g<1>%d\g<2>' % laps, text)
    if n3 < 1:
        raise ValueError('race template: laps not found')
    text= re.sub(r'(<attnum name="(?:focused )?idx" val=")[^"]*(")', r'\g<1>%d\g<2>' % driver_idx, text)
    return text


def parse_track(spec):
    '''"road/corkscrew" -> ("road", "corkscrew"); "corkscrew" -> ("road", "corkscrew").'''
    spec= spec.strip()
    if '/' in spec:
        cat, name= spec.split('/', 1)
    else:
        cat, name= 'road', spec
    if not re.match(r'^[A-Za-z0-9_.-]+$', cat) or not re.match(r'^[A-Za-z0-9_.-]+$', name):
        raise ValueError('bad track %r (expected category/name)' % spec)
    return cat, name


def lap_limit_s(name, max_lap_time, factor):
    ref= REFERENCE_LAP_TIME.get(name)
    if factor > 0 and ref:
        return min(max_lap_time, factor * ref)
    return max_lap_time


# ---------------------------------------------------------------- processes

POSIX= os.name == 'posix'


def popen(cmd, log_path, cwd):
    kw= {'start_new_session': True} if POSIX else {}
    with open(log_path, 'w') as logf:
        return subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=cwd, **kw)


def kill_group(p, sig):
    '''Send `sig` to the process group of `p` (POSIX) or kill `p` (Windows).'''
    if POSIX:
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        try:
            p.kill()
        except OSError:
            pass


def stop(p, first_sig):
    '''Stop `p` and its group: `first_sig` to the process, then SIGKILL to the group.'''
    if p is None:
        return
    if p.poll() is None:
        try:
            if POSIX:
                p.send_signal(first_sig)
            else:
                p.terminate()
        except OSError:
            pass
        try:
            p.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
    if POSIX:
        kill_group(p, signal.SIGKILL)  # leftovers (torcs-bin, shell, apptainer children)
    elif p.poll() is None:
        kill_group(p, None)
    try:
        p.wait(KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def read_text(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def run_track(args, cat, name, template_text, workdir, port):
    '''Run one track; returns the per-track result dict.'''
    tag= '%s_%s' % (cat, name)
    xml_path= os.path.join(workdir, 'race_%s.xml' % tag)
    csv_path= os.path.join(workdir, '%s.csv' % tag)
    sim_log= os.path.join(workdir, '%s.sim.log' % tag)
    drv_log= os.path.join(workdir, '%s.driver.log' % tag)
    with open(xml_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(make_race_xml(template_text, cat, name, args.laps, port - BASE_PORT))
    if os.path.exists(csv_path):
        os.remove(csv_path)

    limit_s= args.laps * lap_limit_s(name, args.max_lap_time, args.time_limit_factor)
    max_steps= int(math.ceil((limit_s + START_MARGIN_S) * STEPS_PER_S))
    score_limit_s= max_steps / float(STEPS_PER_S)  # unfinished base >= any finished time

    sim_cmd= shlex.split(args.sim) + ['torcs', '-r', xml_path]
    drv_cmd= args.python_cmd + ['-u', args.driver, '--params', args.params,
                                '--log', csv_path, '-m', str(max_steps), '-p', str(port)]
    t0= time.monotonic()
    sim= drv= None
    failure= None
    connected= False
    try:
        try:
            sim= popen(sim_cmd, sim_log, workdir)
        except OSError as why:
            with open(sim_log, 'a') as f:
                f.write('cannot start simulator: %s\n' % why)
            failure= 'sim_error'
        if failure is None:
            time.sleep(args.sim_start_wait)
            if sim.poll() is not None:
                failure= 'sim_error'
        if failure is None:
            drv= popen(drv_cmd, drv_log, workdir)
            t_drv= time.monotonic()
            t_sim_exit= None
            while drv.poll() is None:
                now= time.monotonic()
                el= now - t_drv
                # Simulator gone while the driver still waits for data (spec 0016): the
                # driver would block in recvfrom until the wall timeout. A normal race end
                # also stops the simulator, so give the driver a short grace to exit itself.
                if t_sim_exit is None and sim.poll() is not None:
                    t_sim_exit= now
                if (t_sim_exit is not None and now - t_sim_exit > SIM_EXIT_GRACE_S
                        and 'Server has stopped the race' not in read_text(drv_log)):
                    # Before the connection it is a start problem (log marker still wins below).
                    failure= 'sim_crash' if connected else 'sim_error'
                    break
                if not connected:
                    connected= 'Client connected' in read_text(drv_log)
                    if not connected and el > args.connect_timeout:
                        failure= 'no_connect'
                        break
                if el > args.wall_timeout:
                    failure= 'timeout'
                    break
                time.sleep(POLL_S)
            if failure is None and drv.returncode != 0:
                failure= 'driver_error'
    finally:
        # SIGINT lets the driver run its `finally` and flush the CSV.
        stop(drv, signal.SIGINT)
        stop(sim, signal.SIGTERM)
    wall= time.monotonic() - t0
    sim_exit_code= sim.returncode if sim is not None else None

    if SIM_CRASH_MARKER in read_text(sim_log):
        failure= 'sim_crash'
    server_stopped= 'Server has stopped the race' in read_text(drv_log)
    res= evaluate_csv(csv_path, args.laps, score_limit_s, server_stopped, failure,
                      args.k_damage, args.k_offtrack)
    res.update({'track': '%s/%s' % (cat, name), 'wall_s': wall, 'max_steps': max_steps,
                'server_stopped': server_stopped, 'sim_exit_code': sim_exit_code})
    if failure is not None and os.path.exists(csv_path):
        # Keep the partial metrics for diagnostics (score stays FAIL_SCORE unless sim_crash).
        rows= read_telemetry(csv_path)
        if rows and len(rows) >= MIN_ROWS:
            partial= analyze(rows, args.laps, server_stopped)
            for k, v in partial.items():
                res.setdefault(k, v)
    if args.keep_logs:
        res['files']= {'xml': xml_path, 'csv': csv_path, 'sim_log': sim_log, 'driver_log': drv_log}
    else:
        for p in (xml_path, csv_path, sim_log, drv_log):
            try:
                os.remove(p)
            except OSError:
                pass
    return res


# ---------------------------------------------------------------- output

def fmt_line(r):
    if 'laps_done' in r:
        return ('%-22s %-12s laps %d/%d time %7.2f dmg %6.0f off %6.2f s prog %.3f vmax %5.1f -> %9.2f  (wall %.1f s)'
                % (r['track'], r['status'], r['laps_done'], r.get('laps', 0), r['lap_time_total'],
                   r['damage'], r['offtrack_s'], r['progress'], r['max_speed'], r['score'], r.get('wall_s', 0.0)))
    return '%-22s %-12s no data -> %9.2f  (wall %.1f s)' % (r['track'], r['status'], r['score'], r.get('wall_s', 0.0))


def write_json(path, data):
    folder= os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    tmp= path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def constants(args=None):
    '''Scoring constants; K_DAMAGE/K_OFFTRACK are the effective values (CLI overrides).'''
    kd= args.k_damage if args is not None and args.k_damage is not None else K_DAMAGE
    ko= args.k_offtrack if args is not None and args.k_offtrack is not None else K_OFFTRACK
    return {'K_DAMAGE': kd, 'K_OFFTRACK': ko, 'UNFINISHED_PENALTY': UNFINISHED_PENALTY,
            'FAIL_SCORE': FAIL_SCORE, 'SIM_CRASH_PENALTY': SIM_CRASH_PENALTY,
            'SIM_EXIT_GRACE_S': SIM_EXIT_GRACE_S, 'OFFTRACK_TRACKPOS': OFFTRACK_TRACKPOS,
            'FINISH_PROGRESS_MIN': FINISH_PROGRESS_MIN, 'START_MARGIN_S': START_MARGIN_S}


def load_effective_params(path):
    '''Validate the params file with the driver's own loader; returns the
    effective dict. Raises ValueError on a bad file.'''
    sys.path.insert(0, HERE)
    try:
        import snakeoil3_gym
    finally:
        sys.path.pop(0)
    return snakeoil3_gym.load_params(path)


# ---------------------------------------------------------------- main

def build_parser():
    ap= argparse.ArgumentParser(
        description='Evaluate one driver parameter set on several tracks; prints FITNESS <value> (lower = better).')
    ap.add_argument('--params', help='driver params JSON (required unless --score-csv)')
    ap.add_argument('--tracks', default=','.join(DEFAULT_TRACKS),
                    help='comma-separated category/name list [%(default)s]')
    ap.add_argument('--laps', type=int, default=1, help='laps per track [%(default)s]')
    ap.add_argument('--sim', default='',
                    help='prefix command for the simulator; runs "<sim> torcs -r <xml>" (shlex split) [torcs on PATH]')
    ap.add_argument('--python', default=None,
                    help='command to run the driver (shlex split) [this interpreter]')
    ap.add_argument('--driver', default=os.path.join(HERE, 'snakeoil3_gym.py'), help='driver script [%(default)s]')
    ap.add_argument('--race-template', default=os.path.join(HERE, 'quickrace.xml'),
                    help='race xml template [%(default)s]')
    ap.add_argument('--workdir', default=None, help='work directory [./fitness_runs/<timestamp>_<pid>]')
    ap.add_argument('--out', default=None, help='result JSON [<workdir>/result.json]')
    ap.add_argument('--port', type=int, default=BASE_PORT,
                    help='scr_server UDP port %d..%d (sets driver idx in the race xml) [%%(default)s]'
                    % (BASE_PORT, BASE_PORT + MAX_SCR_IDX))
    ap.add_argument('--max-lap-time', type=float, default=DEFAULT_MAX_LAP_TIME,
                    help='sim time limit per lap, s [%(default)s]')
    ap.add_argument('--time-limit-factor', type=float, default=DEFAULT_TIME_LIMIT_FACTOR,
                    help='per-lap limit = min(max-lap-time, factor*reference lap) for known tracks; 0 = off [%(default)s]')
    ap.add_argument('--sim-start-wait', type=float, default=DEFAULT_SIM_START_WAIT,
                    help='s between starting torcs and the driver [%(default)s]')
    ap.add_argument('--connect-timeout', type=float, default=DEFAULT_CONNECT_TIMEOUT,
                    help='s wall for the driver to connect [%(default)s]')
    ap.add_argument('--wall-timeout', type=float, default=DEFAULT_WALL_TIMEOUT,
                    help='s wall per track for the driver [%(default)s]')
    ap.add_argument('--k-damage', type=float, default=None,
                    help='score s per damage point [%s]' % K_DAMAGE)
    ap.add_argument('--k-offtrack', type=float, default=None,
                    help='score s per second off track [%s]' % K_OFFTRACK)
    ap.add_argument('--keep-logs', action='store_true', help='keep race xml, CSV and logs in workdir')
    ap.add_argument('--score-csv', action='append', metavar='CSV',
                    help='offline: score an existing telemetry CSV (repeatable), no simulator')
    return ap


def main(argv=None):
    ap= build_parser()
    args= ap.parse_args(argv)
    if args.laps < 1:
        ap.error('--laps must be >= 1')
    if args.max_lap_time <= 0:
        ap.error('--max-lap-time must be > 0')
    for opt, val in (('--k-damage', args.k_damage), ('--k-offtrack', args.k_offtrack)):
        if val is not None and not (math.isfinite(val) and val >= 0):
            ap.error('%s must be a finite number >= 0' % opt)

    if args.score_csv:
        limit_s= args.laps * args.max_lap_time + START_MARGIN_S
        results= []
        for p in args.score_csv:
            r= evaluate_csv(p, args.laps, limit_s, k_damage=args.k_damage, k_offtrack=args.k_offtrack)
            r.update({'track': os.path.basename(p), 'laps': args.laps})
            results.append(r)
            print(fmt_line(r))
        total= sum(r['score'] for r in results)
        print('FITNESS %.6f' % total)
        if args.out:
            write_json(os.path.abspath(args.out), {'mode': 'score-csv', 'laps': args.laps,
                       'constants': constants(args), 'tracks': results, 'fitness': total})
        return 0

    if not args.params:
        ap.error('--params is required')
    if not (BASE_PORT <= args.port <= BASE_PORT + MAX_SCR_IDX):
        ap.error('--port must be in %d..%d' % (BASE_PORT, BASE_PORT + MAX_SCR_IDX))
    try:
        tracks= [parse_track(t) for t in args.tracks.split(',') if t.strip()]
    except ValueError as why:
        ap.error(str(why))
    if not tracks:
        ap.error('--tracks is empty')
    args.params= os.path.abspath(args.params)
    args.driver= os.path.abspath(args.driver)
    try:
        effective= load_effective_params(args.params)
    except ValueError as why:
        ap.error(str(why))
    try:
        with open(args.race_template, encoding='utf-8') as f:
            template_text= f.read()
        make_race_xml(template_text, 'road', 'x', args.laps, 0)
    except (OSError, ValueError) as why:
        ap.error('race template %s: %s' % (args.race_template, why))
    if not os.path.isfile(args.driver):
        ap.error('driver not found: %s' % args.driver)
    args.python_cmd= shlex.split(args.python) if args.python else [sys.executable]
    workdir= os.path.abspath(args.workdir or os.path.join(
        'fitness_runs', '%s_%d' % (time.strftime('%Y%m%d-%H%M%S'), os.getpid())))
    os.makedirs(workdir, exist_ok=True)
    out= os.path.abspath(args.out or os.path.join(workdir, 'result.json'))
    if args.sim and shutil.which(shlex.split(args.sim)[0]) is None:
        print('warning: %s not found on PATH' % shlex.split(args.sim)[0])

    # SLURM sends SIGTERM on time limit: turn it into an exception so the
    # `finally` blocks kill torcs/driver (they run in their own sessions).
    if POSIX:
        def on_term(signum, frame):
            raise SystemExit(143)
        signal.signal(signal.SIGTERM, on_term)

    t0= time.time()
    results= []
    for cat, name in tracks:
        r= run_track(args, cat, name, template_text, workdir, args.port)
        r['laps']= args.laps
        results.append(r)
        print(fmt_line(r))
        sys.stdout.flush()
    total= sum(r['score'] for r in results)
    data= {
        'params_file': args.params,
        'params': effective,
        'tracks_arg': ['%s/%s' % t for t in tracks],
        'laps': args.laps,
        'port': args.port,
        'sim': args.sim,
        'max_lap_time': args.max_lap_time,
        'time_limit_factor': args.time_limit_factor,
        'constants': constants(args),
        'tracks': results,
        'fitness': total,
        'started': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t0)),
        'wall_s': time.time() - t0,
        'workdir': workdir,
    }
    write_json(out, data)
    if not args.keep_logs:
        try:
            os.rmdir(workdir)  # only succeeds if empty (result.json written elsewhere)
        except OSError:
            pass
    print('result: %s' % out)
    print('FITNESS %.6f' % total)
    return 0


if __name__ == '__main__':
    sys.exit(main())
