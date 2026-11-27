# 0010 — Подбор параметров драйвера CMA-ES на HPC PERUN

| Поле    | Значение                          |
|---------|-----------------------------------|
| Статус  | done                              |
| Автор   | Hlib                              |
| Дата    | 2026-11-26                        |
| Файлы   | `train_cma.py` (новый), `params/search_space.json` (новый), `hpc/train_cma.sbatch` (новый), `hpc/setup_pylib.sh` (новый), `hpc/README.md` (новый), `hpc/torcs-env.def` (новый в репо, копия рецепта образа с кластера), `.gitattributes` (новый), `.gitignore`, `requirements.txt` |
| Коммиты | 5751b57                           |

## Запрос / мотивация
После 0009 есть `fitness.py`: один набор параметров → одно число (сумма «эквивалентных» времён круга на
8 трассах, меньше = лучше, детерминированно, ~17 с на оценку). Нужен оптимизатор, который подбирает
параметры `snakeoil3_gym.py` эволюционной стратегией CMA-ES на SLURM-кластере PERUN: параллельно до 10
оценок на узел (у `scr_server` порты 3001..3010), с логами по поколениям, лучшим набором в формате
`--params`, контрольной точкой, продолжением после лимита времени SLURM и корректной остановкой.
Плюс скрипты для кластера (задача SLURM, установка numpy/cma в образ без pip) и рецепт образа в репо.

`snakeoil3_gym.py` и `fitness.py` **не меняются** — интерфейса `fitness.py` (`--params/--port/--workdir/
--out`, `FITNESS` в последней строке, JSON с `fitness`) достаточно.

## Что меняем

### `train_cma.py` (новый; нужны numpy + cma, остальное — stdlib)
- **Пространство поиска** — `params/search_space.json`: `{param: [low, high]}` (ключи на `_` — комментарии).
  Имена проверяются по `DEFAULT_PARAMS`; целочисленный параметр — тот, у кого значение в `DEFAULT_PARAMS`
  типа `int` (`ahead_halfwidth`). Остальные параметры фиксированы из `--base` (по умолчанию
  `params/default.json`, читается `snakeoil3_gym.load_params`).
- **CMA-ES (pycma)** в нормированном кубе `[0,1]^n`: `x = (v − low)/(high − low)`, опция `bounds=[0,1]`
  (стандартная `BoundTransform`), `x0` = нормированный `--base` (вне границ → обрезка + предупреждение),
  `--sigma0` 0.2, `--popsize` 20, `--seed`, доп. опции `--cma-opts '{"...": ...}'`. `verbose=-9`,
  `verb_log=0` — pycma не пишет свои файлы. Кандидат → значения обрезаются в `[low, high]`, целые
  округляются; в CMA сообщается исходный `x`.
- **Параллельные оценки:** `ThreadPoolExecutor` на K = `--workers` (≤ 10) потоков + очередь портов;
  каждая оценка берёт свободный порт `3001+i`, пишет `work/p<port>_params.json` (полный словарь) и запускает
  `python3 -u fitness.py --params … --port P --workdir work/p<port> --out work/p<port>_result.json --laps N
  [--tracks …] [--fitness-args …]` (свой session/группа процессов). Фитнес — из JSON (`fitness`), запасной
  вариант — последняя строка `FITNESS x` в логе. Сбой (код ≠ 0, нет числа, таймаут `--eval-timeout`, по
  умолчанию `150 с · трассы · круги + 60`) → `--fail-value` (1e6; максимум `fitness.py` — 8·10000), лог
  сохраняется в `failed/<gen>_<idx>.log` (не более 200). Таймаут/остановка: `fitness.py` получает SIGTERM
  (он сам гасит torcs и драйвер в `finally`), через 10 с — SIGKILL группе.
- **Блокировка портов:** `flock` на `/tmp/scr_port_<port>.lock` (`--lock-dir`) на всё время работы — если
  SLURM посадит две задачи на один узел (`-c 12` — это часть узла), они не займут один порт: вторая
  возьмёт свободные порты или меньше воркеров (предупреждение). На Windows — без блокировок.
- **Поколение 0** — оценка `--base` (опорное значение, строка `gen 0` в CSV, `best.json` = base). Если она
  не удалась — выход с кодом 1 (инфраструктура сломана, нет смысла жечь 47 ч).
- **Выходы в `--outdir`:**
  - `generations.csv` — `gen, evals, best, median, worst, sigma, best_so_far, wall_s` (wall — суммарно по всем запускам);
  - `candidates.jsonl` — на каждого кандидата: `gen, idx, fitness, status, wall_s, port, params`
    (только оптимизируемые; фиксированные — в `base.json`), `tracks` (трасса, статус, балл, время, урон, вне трассы);
  - `best.json` — **полный** словарь параметров (сразу для `snakeoil3_gym.py --params`), обновляется при
    улучшении; `best_result.json` — JSON `fitness.py` для него;
  - `checkpoint.pkl` — pickle: объект `es`, номер поколения, счётчики, лучший результат, base, пространство,
    трассы/круги, состояние `numpy.random`; пишется атомарно после каждого поколения;
  - `run_config.json`, `base.json`, `failed/`, `work/`.
- **Остановка:** `--max-gens`; `--max-hours` (новое поколение не начинается, если оставшееся время
  < 1.2 · длительность прошлого); критерии pycma (`es.stop()`); SIGTERM/SIGINT — недоделанное поколение
  отбрасывается (в CMA не передаётся), запущенные оценки останавливаются, выход с кодом 0; контрольная
  точка = конец последнего полного поколения.
- **`--resume`:** продолжает из `checkpoint.pkl`; пространство поиска должно совпадать (иначе ошибка),
  base берётся из контрольной точки, `--popsize/--sigma0/--seed/--base` игнорируются; если трассы/круги
  другие — предупреждение. Без `--resume` при существующем `checkpoint.pkl` — ошибка (не затереть прогон).
- Одна строка на поколение в stdout:
  `gen 12 evals 241 best … median … worst … sigma … best_so_far … * fail 0 gen 38.2 s wall 470 s` (`*` — улучшение).
- **Для проверки без кластера:** `--dry-run` (без numpy/cma: таблица пространства, base + 3 случайных
  кандидата в `<outdir>/dry_run/`, проверка через `load_params`, обратная нормировка); `--fitness-script` —
  подменить оценщик скриптом с теми же аргументами; `--python` — чем запускать оценщик.

### Пространство поиска (`params/search_space.json`, 18 параметров)
Значения по умолчанию не меняются — это границы поиска; все дефолты внутри.

| Параметр | Дефолт | Границы | Почему так |
|----------|--------|---------|------------|
| `v_corner` | 80 | 40 … 160 км/ч | Главный параметр скорости в повороте. Ниже 40 — ползём, выше 160 — физика торможения уже не спасает в шпильках |
| `decel` | 12 | 6 … 30 м/с² | Принятое замедление (0.6–3 g): у формулы с прижимной силой реальное торможение на скорости 2–3 g |
| `dist_margin` | 15 | 0 … 40 м | Запас до препятствия; > 40 м — сильно раннее торможение |
| `ahead_halfwidth` | 2 (int) | 0 … 5 | Сколько лучей вокруг центрального брать (±0 … ±4°); дальше — уже лучи поворота |
| `curv_speed_gain` | 0 | 0 … 2 км/ч на ° | Снижение цели по направлению длинного луча; при 45° и 2 → −90 км/ч. Отрицательные бессмысленны |
| `accel_gain` | 0.1 | 0.03 … 0.5 | Газ на км/ч недобора: от мягкого до почти «вкл/выкл» (2 км/ч → полный) |
| `brake_gain` | 0.1 | 0.02 … 0.5 | То же для тормоза (50 … 2 км/ч перебора до полного) |
| `abs_slip` | 0.15 | 0.05 … 0.4 | Порог проскальзывания ABS; пик сцепления шин обычно 0.1–0.2 |
| `abs_gain` | 3 | 0 … 10 | 0 = ABS выключен (пусть оптимизатор решит), 10 — резкий сброс |
| `tcs_slip` | 0.15 | 0.05 … 0.4 | Порог буксования TCS |
| `tcs_gain` | 3 | 0 … 10 | 0 = TCS выключен |
| `steer_angle_gain` | 4.775 | 1.5 … 8 | Руль по углу к оси; < 1.5 не довернуть в повороте, > 8 — раскачка |
| `steer_pos_gain` | 0.10 | 0 … 0.6 | Тяга к центру; 0 — разрешить ехать по траектории, не по оси |
| `steer_ray_gain` | 0 | 0 … 1 | Руль к самому длинному лучу (≤ 0.785 рад · 1 = 0.79 руля); подрезка поворотов |
| `gear_up_rpm` | 17500 | 15000 … 18600 | Отсечка car1-ow1 — 18700 |
| `gear_down_rpm` | 11000 | 7000 … 13000 | Пересечение с `gear_up_rpm` невозможно (13000 < 15000); «звон» передач при близких значениях оптимизатор сам накажет временем |
| `offtrack_speed` | 50 | 20 … 100 км/ч | Скорость возврата на трассу |
| `offtrack_pos_gain` | 0.5 | 0.1 … 1 | Руль к центру вне трассы; 0 — не вернуться |

Не оптимизируем (фиксированы из `--base`):
- `v_max` (300) — только ограничитель сверху, car1-ow1 на наших трассах до него почти не разгоняется;
- `v_min` (50) — нижний предел; при `d_ahead > dist_margin` цель и так ≥ `v_corner`, работает только вместе с
  `curv_speed_gain` — пусть останется полом;
- `brake_hyst` (2) — мёртвая зона, почти полностью дублирует `accel_gain`/`brake_gain`;
- `wheel_radius_front/rear` — геометрия машины (измерено в 0008), не настройка;
- `abs_min_speed`, `tcs_min_speed` — влияют только ниже 10 км/ч (старт, разворот);
- `gear_max` — 6 передач нужны на прямых, меньше — только потеря скорости;
- `stuck_*`, `reverse_*` — аварийная логика, на чистом заезде не срабатывает, фитнес почти не чувствует, а
  шум добавит.

18 параметров — нормальный размер для CMA-ES с популяцией 20 (по умолчанию pycma для n=18: 4+3·ln18 ≈ 12;
20 — с запасом под негладкий фитнес и кратно 10 воркерам).

### `hpc/train_cma.sbatch` (новый, LF)
`cpu_short`, account/qos — из `SBATCH_ACCOUNT`/`SBATCH_QOS` (в заголовке номера проекта нет), 1 узел, `-c 12` (10 оценок, каждая ≈ 1 ядро: torcs и
драйвер работают поочерёдно по UDP; + запас), `--mem=16G`, `-t 47:00:00`, лог
`/mnt/scratch/<user>/cma_logs/<job>-<id>.out`. Переменные: `PROJECT` (`/mnt/project/$SLURM_JOB_ACCOUNT`), `CODE_DIR` (`/mnt/scratch/$USER/gym_torcs`),
`OUTDIR` (`/mnt/scratch/$USER/cma_runs/run_<jobid>`), `SIF`, `PYLIB`, `MAX_HOURS` (46.5), `WORKERS` (10);
аргументы после имени скрипта уходят в `train_cma.py`. `HOME=/mnt/scratch/$USER/torcs_home` (TORCS пишет
`~/.torcs`), `PYTHONPATH=$PYLIB`. Если в `OUTDIR` есть `checkpoint.pkl` — сам добавляет `--resume`
(одна и та же команда продолжает прогон, можно цепочкой `--dependency=afterany`).
Запуск: `exec apptainer exec --home $HOME --bind /mnt/scratch/$USER,$PROJECT $SIF
python3 -u train_cma.py …` (`exec` — SIGTERM от SLURM сразу доходит до apptainer → `train_cma.py`).
`fitness.py` внутри образа запускает `torcs` из образа (`--sim` пустой) — как в проверенном прогоне 0009.

### `hpc/setup_pylib.sh` (новый, LF)
На логин-узле (python3 3.9 хоста): `pip install --upgrade --only-binary=:all: --python-version 3.10
--platform manylinux2014_x86_64 --implementation cp --target <pylib> numpy==2.0.2 cma==4.6.0`, затем проверка
`import numpy, cma` внутри образа. По умолчанию `<pylib>` = `/mnt/project/$PERUN_PROJECT/torcs/pylib`.

### `hpc/torcs-env.def` (в репо впервые)
Рецепт образа, по которому собран `/mnt/project/<project>/torcs/torcs-env.sif`: Ubuntu 22.04,
TORCS 1.3.7 (`fmirus/torcs-1.3.7`) в `/usr/local/torcs`, `scr_server` из `ibm-ai-racing/racing-league`,
Python 3.10 (`python3` из Ubuntu, без pip). Сборка — на вычислительном узле:
```
export APPTAINER_CACHEDIR=/mnt/scratch/$USER/apptainer_cache APPTAINER_TMPDIR=/mnt/scratch/$USER/apptainer_tmp
apptainer build --fakeroot /mnt/project/$PERUN_PROJECT/torcs/torcs-env.sif hpc/torcs-env.def
```

### `hpc/README.md` (новый)
Кратко: образ, pylib, копирование кода (`scp`), `--dry-run`, `sbatch`, продолжение, `squeue`/`tail`,
забрать `best.json`.

### Прочее
- `.gitattributes` (новый): `*.sh`, `*.sbatch`, `hpc/*.def` — `text eol=lf` (у Hlib `core.autocrlf=true`,
  без этого после checkout на Windows скрипты получат CRLF и при `scp` сломаются в bash/SLURM). Все
  отслеживаемые `.sh` и так хранятся с LF — индекс не меняется; `.def` из `vtorcs-RL-color/` не затронуты
  (маска только `hpc/`).
- `.gitignore`: `cma_runs/` (папка по умолчанию).
- `requirements.txt`: `cma==4.6.0` с пометкой «только для обучения» (правило соревнования — внешние
  библиотеки документируются). На кластере — numpy 2.0.2 + cma 4.6.0 (numpy 2.4 не поддерживает Python 3.10);
  локально numpy остаётся 2.4.6. Драйвер и `fitness.py` по-прежнему только stdlib.

### Параметры драйвера
Не меняются (меняется только то, что подбирает оптимизатор, — в будущих спеках с таблицей «было → стало»).

## CLI
```
python3 train_cma.py [--outdir DIR] [--resume] [--base params/default.json] [--space params/search_space.json]
    [--popsize 20] [--sigma0 0.2] [--seed N] [--cma-opts JSON] [--workers 10]
    [--max-gens 0] [--max-hours 0] [--tracks a/b,c/d] [--laps 1] [--fitness-args "..."]
    [--eval-timeout 0] [--fail-value 1e6] [--lock-dir /tmp] [--python CMD] [--fitness-script fitness.py]
    [--dry-run]
```

## Ожидаемая производительность
Оценка на 8 трассах ≈ 17 с (0009). 10 воркеров, popsize 20 → 2 волны ≈ 35–40 с на поколение
(медленные/зависшие кандидаты задерживают волну — ограничены `--time-limit-factor` в `fitness.py` и
`--eval-timeout`). За 46.5 ч ≈ 4000+ поколений ≈ 85 000 оценок; `candidates.jsonl` ≈ 1 КБ на строку → ~85 МБ.
Если поколение заметно дольше 40 с — не хватает ядер: попробовать `-c 20`.

## Как проверить
Локально (сделано, Windows, Python 3.11, numpy 2.4.6; pycma локально нет — вместо неё тестовая заглушка
с тем же API `ask/tell/stop/sigma` через `PYTHONPATH`, вместо `fitness.py` — скрипт-квадратичная функция с
теми же аргументами через `--fitness-script`, со специально падающими (код 3) и зависающими кандидатами):
```
python -m py_compile train_cma.py
python train_cma.py --help
python train_cma.py --dry-run --outdir <tmp>/dry
python train_cma.py --outdir <tmp>/run1 --fitness-script <dummy> --popsize 12 --workers 4 --max-gens 3 --seed 7 ...
python train_cma.py --outdir <tmp>/run1 ... --max-gens 5 --resume
```

На PERUN:
0. В `~/.bashrc` на кластере: `PERUN_PROJECT`, `SBATCH_ACCOUNT`, `SBATCH_QOS` (`hpc/README.md`, п. 0).
1. `python3 train_cma.py --dry-run --outdir /mnt/scratch/$USER/cma_runs/dry` (логин-узел).
2. `bash hpc/setup_pylib.sh` (если pylib ещё нет) — печатает `numpy 2.0.2 cma 4.6.0` из образа.
3. Короткий прогон: `mkdir -p /mnt/scratch/$USER/cma_logs; OUTDIR=/mnt/scratch/$USER/cma_runs/test
   sbatch -t 00:20:00 hpc/train_cma.sbatch --max-gens 2` → в логе gen 0 ≈ **976.6** (как в 0009), 2 строки
   поколений ≈ 35–40 с каждая, `fail 0` (или единицы — смотреть `failed/`), `best.json` читается
   `snakeoil3_gym.py --params`.
4. Продолжение: та же команда ещё раз → в логе `resumed … gen 2`, затем gen 3.
5. `scancel` во время поколения → в логе `gen N discarded (stopping)`, `stopped: signal 15`, нет
   оставшихся `torcs-bin` (`ssh <node> pgrep -u $USER torcs` до конца задачи).
6. Полный прогон: `OUTDIR=/mnt/scratch/$USER/cma_runs/run1 sbatch hpc/train_cma.sbatch`.

Успех: `best_so_far` монотонно падает ниже 976.6, на лучшем наборе все 8 трасс `finished`.

## Результат
Локальная проверка (2026-11-26, Windows, заглушка pycma + тестовый оценщик):
- `py_compile` — OK; `--help` — OK.
- `--dry-run`: 18 параметров, x0 = нормированный default (`curv_speed_gain`, `steer_ray_gain` = 0 на нижней
  границе); 4 файла кандидатов приняты `load_params` (32 параметра, `ahead_halfwidth` — `int`);
  обратная нормировка совпадает (кроме округлённого `ahead_halfwidth`).
- Цикл: gen 0 (base) → 3 поколения, `best_so_far` 987.1 → 911.1; `generations.csv`, `candidates.jsonl`,
  `best.json` (полный, читается `load_params`), `best_result.json`, `checkpoint.pkl`, `run_config.json`.
- `--resume` → продолжил с gen 4, CSV дописан; без `--resume` при существующей контрольной точке — ошибка (код 2).
- Сбои (`--sigma0 0.8`, 10 воркеров): 41 оценка — 27 ok, 10 `exit 3`, 4 `timeout` (оценщик убит за `--eval-timeout`),
  они получили 1e6, логи в `failed/`.
- SIGINT посреди поколения 2 → `gen 2 discarded`, выход 0 за 0.4 с; `--resume` → поколение 2 заново, дальше 3.
- `--max-hours 0.0015` → остановка перед поколением, которое не успевало.

Проверка на PERUN (2026-11-26, задачи 177001 + продолжение, `hpc/train_cma.sbatch`, numpy 2.0.2 + cma 4.6.0
из pylib, 10 воркеров, `--popsize 10`, 4 трассы: corkscrew, aalborg, g-track-1, ruudskogen):
- `bash -n` для `train_cma.sbatch` и `setup_pylib.sh` — OK.
- gen 0 (default) = **382.978** — совпадает с суммой баллов этих трасс из 0009 (109.25 + 126.56 + 55.92 + 91.25).
- Поколение (10 оценок × 4 трассы параллельно на портах 3001–3010) — **≈11 с**; сбоев 0.
- Поколения 1–3 → `best_so_far` 360.804 (gen 2); повторный `sbatch` с тем же `OUTDIR` сам добавил `--resume`,
  продолжил с gen 3 (настоящая pycma из pickle) и досчитал 4–5. `generations.csv` дописан без разрывов.
- `best.json` читается `load_params` драйвера (32 параметра).
- Лучший кандидат (360.80 против 382.98) по трассам: corkscrew 109.06 (было 109.25), aalborg **93.32**
  (было 126.56: урон 1204 → 6, вне трассы 17.2 → 0.3 с), g-track-1 60.69 (было 55.92), ruudskogen 97.73
  (было 91.25). Выигрыш почти целиком за счёт чистого aalborg, остальные трассы медленнее — 5 поколений
  только проверка механики, а не обучение.

Конфликт портов проверен случайно: пробная задача попала на узел с идущим длинным прогоном (10 воркеров) и
сразу завершилась с `no free scr_server port (all 3001..3010 locked in /tmp)` — блокировки `flock` работают
между задачами. Обход — `sbatch --exclusive` (в `hpc/README.md`); та же задача с `--exclusive` на свободном
узле: gen 0 = 147.166 (g-track-1 + ruudskogen, как в 0009), gen 1 без сбоев. Account/qos при этом заданы
только через `SBATCH_ACCOUNT`/`SBATCH_QOS`, путь к проекту выведен из `SLURM_JOB_ACCOUNT`.

Не проверено: остановка по SIGTERM/`scancel` на кластере, длинный прогон (запущен).

## Заметки / открытые вопросы
- Если SLURM даёт задаче свой `/tmp` (плагин job_container/tmpfs), блокировки портов между задачами не
  работают — тогда `--lock-dir /dev/shm` или `#SBATCH --exclusive`.
- `BoundTransform` у pycma: значения у самых границ достигаются «вязко»; параметры с дефолтом на границе
  (`curv_speed_gain`, `steer_ray_gain` = 0) стартуют в углу — поиск идёт внутрь, это нормально.
- Детерминированный фитнес → лучший кандидат не «везучий», но переобучение на 8 трасс возможно: проверить
  `best.json` на трассах вне набора (на соревновании трассы случайные).
- Следующий шаг — запустить, по результату — спека с новым `params/*.json` и таблицей «было → стало».
