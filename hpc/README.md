# HPC PERUN: подбор параметров CMA-ES

Подробности и обоснования — `specs/0010-cma-es-training.md`.

| Файл | Что это |
|------|---------|
| `torcs-env.def` | Рецепт образа apptainer: Ubuntu 22.04, TORCS 1.3.7 + `scr_server`, Python 3.10 |
| `setup_pylib.sh` | Ставит `numpy` + `cma` в общую папку `pylib` (в образе нет pip) |
| `train_cma.sbatch` | Задача SLURM: `train_cma.py` внутри образа, 10 параллельных оценок |

Пути ниже: `$U` — логин на кластере (scratch: `/mnt/scratch/$U`), `<project>` — номер проекта PERUN
(account/qos, вида `perunNNNNNNNN`; виден в портале), папка проекта — `/mnt/project/<project>`.

## 0. Окружение на кластере (один раз)

Добавить в `~/.bashrc` на кластере:
```
export PERUN_PROJECT=<project>
export SBATCH_ACCOUNT=$PERUN_PROJECT SBATCH_QOS=$PERUN_PROJECT   # sbatch берёт --account/--qos отсюда
```

## 1. Образ (один раз, уже собран)

На вычислительном узле (на логин-узле сборка тяжёлая), кэш и временные файлы — на scratch:
```
export APPTAINER_CACHEDIR=/mnt/scratch/$USER/apptainer_cache
export APPTAINER_TMPDIR=/mnt/scratch/$USER/apptainer_tmp
mkdir -p $APPTAINER_CACHEDIR $APPTAINER_TMPDIR
apptainer build --fakeroot /mnt/project/$PERUN_PROJECT/torcs/torcs-env.sif hpc/torcs-env.def
```

## 2. numpy + cma (один раз, уже сделано)

На логин-узле:
```
bash hpc/setup_pylib.sh            # -> /mnt/project/$PERUN_PROJECT/torcs/pylib
```
В конце скрипт печатает версии изнутри образа (`3.10.x numpy 2.0.2 cma 4.6.0`).

## 3. Код на кластер

С Windows (из корня репо), только нужные файлы:
```
scp snakeoil3_gym.py fitness.py train_cma.py quickrace.xml $U@<perun>:/mnt/scratch/$U/gym_torcs/
scp -r params hpc $U@<perun>:/mnt/scratch/$U/gym_torcs/
```
`.sh`/`.sbatch` должны быть с LF (в репо это закреплено `.gitattributes`; если файл пришёл с CRLF —
`sed -i 's/\r$//' hpc/*.sh hpc/*.sbatch`).

Проверка без симулятора (на логин-узле, numpy/cma не нужны):
```
cd /mnt/scratch/$U/gym_torcs
python3 train_cma.py --dry-run --outdir /mnt/scratch/$U/cma_runs/dry
```

## 4. Запуск

```
mkdir -p /mnt/scratch/$U/cma_logs          # папка для лога SLURM должна существовать заранее
cd /mnt/scratch/$U/gym_torcs
OUTDIR=/mnt/scratch/$U/cma_runs/run1 sbatch hpc/train_cma.sbatch
OUTDIR=/mnt/scratch/$U/cma_runs/run1 sbatch hpc/train_cma.sbatch --popsize 30 --sigma0 0.3
```
Аргументы после имени скрипта уходят в `train_cma.py` (`--help` — список). Переменные окружения
(`OUTDIR`, `CODE_DIR`, `MAX_HOURS`, `WORKERS`, `SIF`, `PYLIB`) по умолчанию передаются в задачу.
Короткий пробный прогон: `sbatch -p cpu_short -t 00:20:00 hpc/train_cma.sbatch --max-gens 2`.

**Вторая задача одновременно:** порты 3001–3010 общие на узел. Если планировщик поставит её на узел,
где уже идёт прогон, она сразу завершится с `no free scr_server port`. Запускать с `sbatch --exclusive ...`
(узел целиком; `--exclude` на PERUN запрещён).

**Продолжение:** тот же `OUTDIR` → в sbatch автоматически добавится `--resume` (если есть
`checkpoint.pkl`). Цепочка задач: `sbatch --dependency=afterany:<jobid> hpc/train_cma.sbatch` с тем же `OUTDIR`.
Для `cpu_long` (4 дня): `sbatch -p cpu_long -t 95:00:00` и `MAX_HOURS=94.5`.

## 5. Наблюдение

```
squeue -u $U
tail -f /mnt/scratch/$U/cma_logs/torcs-cma-<jobid>.out      # строка на поколение
column -s, -t < /mnt/scratch/$U/cma_runs/run1/generations.csv | tail
scancel <jobid>                                               # корректная остановка (SIGTERM)
```
Неудачные оценки — `<OUTDIR>/failed/*.log`.

## 6. Результат обратно

```
scp $U@<perun>:/mnt/scratch/$U/cma_runs/run1/best.json params/cma_run1.json
scp $U@<perun>:/mnt/scratch/$U/cma_runs/run1/best_result.json runs/cma_run1_best_result.json
scp $U@<perun>:/mnt/scratch/$U/cma_runs/run1/generations.csv runs/cma_run1_generations.csv
python snakeoil3_gym.py --params params/cma_run1.json
```
`best.json` — полный набор параметров. Новый файл в `params/` — через спеку (таблица «было → стало»).
