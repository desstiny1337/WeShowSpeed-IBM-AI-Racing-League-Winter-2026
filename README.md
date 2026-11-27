# Gym-TORCS

> **WeShowSpeed — IBM AI Racing League, Winter 2026.**
> Как работать над проектом — [CONTRIBUTING.md](CONTRIBUTING.md). История изменений — [specs/CHANGELOG.md](specs/CHANGELOG.md).
> Third-party code and licenses — [see below](#third-party-code-and-licenses).
>
> **How to run our car:** start TORCS (Race → Practice → New Race, driver `scr_server`), then
> `python torcs_jm_par.py` (official entry point, port 3001). It runs our driver `drive_example()`
> from `snakeoil3_gym.py`; `python snakeoil3_gym.py` does the same. Options: `-p PORT`, `--params FILE`, `--log FILE`.

Gym-TORCS is the reinforcement learning (RL) environment in TORCS domain with OpenAI-gym-like interface.
TORCS is the open-rource realistic car racing simulator recently used as RL benchmark task in several AI studies.

Gym-TORCS is the python wrapper of TORCS for RL experiment with the simple interface (similar, but not fully) compatible with OpenAI-gym environments. The current implementaion is for only the single-track race in practie mode. If you want to use multiple tracks or other racing mode (quick race etc.), you may need to modify the environment, "autostart.sh" or the race configuration file using GUI of TORCS.

This code is developed based on vtorcs (https://github.com/giuse/vtorcs)
and python-client for torcs (http://xed.ch/project/snakeoil/index.html).

The detailed explanation of original TORCS for AI research is given by Daniele Loiacono et al. (https://arxiv.org/pdf/1304.1672.pdf)

Because torcs has memory leak bug at race reset.
As an ad-hoc solution, we relaunch and automate the gui setting in torcs.
Any better solution is welcome!

# Requirements
We are assuming you are using Ubuntu 14.04 LTS/16.04 LTS machine and installed
* Python 3
* xautomation (http://linux.die.net/man/7/xautomation)
* OpenAI-Gym (https://github.com/openai/gym)
* numpy
* vtorcs-RL-color (installation of vtorcs-RL-color is explained in vtorcs-RL-color directory)

# Example Code
The example code and agent are written in example_experiment.py and sample_agent.py.

# Initialization of the Race
After the insallation of vtorcs-RL-color, you need to initialize the race setting. You can find the detailed explanation in a document (https://arxiv.org/pdf/1304.1672.pdf), but here I show the simple gui-based setting.

So first you need to run
```
sudo torcs
```
in the terminal, the GUI of TORCS should be launched.
Then, you need to choose the race track by following the GUI (Race --> Practice --> Configure Race) and open TORCS server by selecting Race --> Practice --> New Race. This should result that TORCS keeps a blue screen with several text information.

If you need to treat the vision input in your AI agent, you have to set the small image size in TORCS. To do so, you have to run
```
python snakeoil3_gym.py
```
in the second terminal window after you open the TORCS server (just as written above). Then the race starts, and you can select the driving-window mode by F2 key during the race.

After the selection of the driving-window mode, you need to set the appropriate gui size. This is done by using the display option mode in Options --> Display. You can select the Screen Resolution, and you need to select 64x64 for visual input (our immplementation only support this screen size, other screen size results the unreasonable visual information). Then, you need to shut down TORCS to complete the configuration for the vision treatment.


# Simple How-To

```python
from gym_torcs import TorcsEnv

#### Generate a Torcs environment
# enable vision input, the action is steering only (1 dim continuous action)
env = TorcsEnv(vision=True, throttle=False)

# without vision input, the action is steering and throttle (2 dim continuous action)
# env = TorcsEnv(vision=False, throttle=True)

ob = env.reset(relaunch=True)  # with torcs relaunch (avoid memory leak bug in torcs)
# ob = env.reset()  # without torcs relaunch

# Generate an agent
from sample_agent import Agent
agent = Agent(1)  # steering only
action = agent.act(ob, reward, done, vision=True)

# single step
ob, reward, done, _ = env.step(action)

# shut down torcs
env.end()
```

# 

# Add Noise in Low-dim Sensors

If you want to apply sensor noise in low-dimensional sensors, you should 

```
os.system('torcs -nofuel -nodamage -nolaptime -vision -noisy &')
os.system('torcs -nofuel -nolaptime -noisy &')
```

at 33 & 35th lines in gym_torcs.py

# Great Application
gym-torcs was utilized in DDPG experiment with Keras by Ben Lau. 
This experiment is really great!

https://yanpanlau.github.io/2016/10/11/Torcs-Keras.html

# Acknowledgement
gym_torcs was developed during the spring internship 2016 at Preferred Networks.

# Third-party code and licenses

This repository is a fork of an open-source project and builds on other open-source work.
Everything not written by our team is listed here.

## Code

| Component | Author / source | License | Where it is used |
|-----------|-----------------|---------|------------------|
| gym_torcs | Naoto Yoshida, [ugo-nama-kun/gym_torcs](https://github.com/ugo-nama-kun/gym_torcs) | MIT (see [LICENSE](LICENSE)) | Upstream of this fork: `gym_torcs.py`, `sample_agent.py`, `example_experiment.py`, `autostart.sh` (unchanged) |
| Snake Oil (Python SCR client) | Chris X Edwards, [xed.ch/project/snakeoil](http://xed.ch/project/snakeoil/index.html); Python 3 port from gym_torcs | see the author's page | `Client`, `ServerState`, `DriverAction` in `snakeoil3_gym.py` (also copied into `snakeoil3_jm2.py`, `jmcncarai.py`) |
| vtorcs-RL-color | TORCS team, [giuse/vtorcs](https://github.com/giuse/vtorcs), RL patches from gym_torcs | GPL v2 (see [vtorcs-RL-color/COPYING](vtorcs-RL-color/COPYING)) | `vtorcs-RL-color/` (vendored, not modified by us) |
| SCR server (`scr_server` robot) | Daniele Loiacono et al., Simulated Car Racing championship; package from [ibm-ai-racing/racing-league](https://github.com/ibm-ai-racing/racing-league) | GPL v2 or later | Simulator side; `hpc/scr_server.xml` is its config with all slots set to `car1-ow1` |
| TORCS 1.3.7 | TORCS team, [fmirus/torcs-1.3.7](https://github.com/fmirus/torcs-1.3.7) | GPL v2 or later | Built inside the HPC container image (`hpc/torcs-env.def`), not stored in this repository |

## Python libraries

| Library | License | Used by |
|---------|---------|---------|
| [numpy](https://numpy.org/) | BSD-3-Clause | `train_cma.py`, `sample_agent.py` |
| [matplotlib](https://matplotlib.org/) | Matplotlib License (PSF-based) | analysis / example agent |
| [cma (pycma)](https://github.com/CMA-ES/pycma) | BSD-3-Clause | `train_cma.py` (CMA-ES parameter search) |
| [gym](https://github.com/openai/gym) (optional) | MIT | `gym_torcs.py` only |

The competition driver itself (`snakeoil3_gym.py`) uses only the Python standard library.
Versions are pinned in [requirements.txt](requirements.txt).

## Written by our team (WeShowSpeed)

- Driving logic in `snakeoil3_gym.py`: `drive_example()` (steering, braking by range finders,
  ABS/TCS, gear shifting by rpm, recovery), `DEFAULT_PARAMS` / `--params`, telemetry logger `--log`.
- `fitness.py` — multi-track evaluation of a parameter set; `train_cma.py` — CMA-ES optimizer.
- `hpc/` — container definition, SLURM scripts and setup for the HPC cluster.
- Race configs `practice.xml`, `quickrace.xml`; process docs `CONTRIBUTING.md` and `specs/`.

Change history with motivation for each step: [specs/CHANGELOG.md](specs/CHANGELOG.md).
