# 0016 — Detect simulator crashes in fitness.py, tunable penalty weights

| Поле    | Значение                          |
|---------|-----------------------------------|
| Статус  | in-progress                       |
| Автор   | Hlib                              |
| Дата    | 2026-11-27                        |
| Файлы   | `fitness.py`                      |
| Коммиты | (заполняет git-агент)             |

## Запрос / мотивация
Перед следующим обучением CMA-ES (run4) `fitness.py` должен правильно обрабатывать падение симулятора
и позволять менять веса штрафов без правки кода.

Что известно (PERUN, 2026-11-27, заметки спеки 0015):
- На `road/e-track-1` TORCS 1.3.7 (`torcs-bin`) **детерминированно падает (segfault) около 785 м**, если драйвер
  рулит по дальномерам (`steer_ray_gain`): run3 (2.99), половина этого значения, даже параметры 0008 + этот
  `steer_ray_gain` — всё падает.
- Зависит от траектории (левый край после поворота), а не от скорости: с ограничением `v_max` 250..300 тоже падает.
  Без ray-руления круг проходится (94.4 с).
- В логе симулятора: `Segmentation fault (core dumped) $LIBDIR/torcs-bin ...`.
- После падения драйвер висит в `recvfrom` (клиент snakeoil ждёт данных бесконечно, таймаут сокета 1 с
  и повтор) до `--wall-timeout` `fitness.py` (120 с) → статус `timeout`, балл `FAIL_SCORE` = 10000.
  Итог: на трассе теряется 2 минуты, а балл одинаковый для падения на 100 м и на 3000 м — CMA-ES не видит,
  какой набор «почти проехал».

Кроме того, лучший набор run3 получил урон до 259 на незнакомой alpine-2 (вес урона 0.01 с/очко почти не
наказывает это), поэтому веса урона/выездов нужно задавать из командной строки (`train_cma.py --fitness-args`).

## Причина (насколько известно)
- Падение внутри `torcs-bin` (C++), не в Python. Триггер — траектория, которую даёт `steer_ray_gain`.
  Точное место в коде TORCS не найдено (на PERUN нет отладочной сборки/core dump не разобран).
- Путь пит-лейна в `raceengine.cpp` исключён: он требует `RM_CMD_PIT_ASKED` и |v| < 1 м/с, а машина
  в момент падения едет.
- `torcs` — shell-скрипт-обёртка (`src/linux/torcs.in`): он запускает `$LIBDIR/torcs-bin ...` как дочерний
  процесс (не `exec`) и затем делает `exit 0`. Поэтому при segfault `torcs-bin` shell печатает
  `Segmentation fault (core dumped) ...` в свой stderr (= лог симулятора у `fitness.py`) и сам завершается
  с кодом 0. Значит, процесс, который запускает `fitness.py` (обёртка `torcs` или `apptainer exec ... torcs`),
  завершается сразу после падения — это и ловим через `Popen.poll()`. Код возврата для распознавания
  не годится (0), поэтому дополнительно смотрим текст лога.

## Что меняем (`fitness.py`)
1. **Обнаружение падения во время заезда.** В цикле ожидания драйвера (каждые `POLL_S` = 0.05 с) проверяется
   и процесс симулятора. Если симулятор завершился, а драйвер ещё работает, ждём `SIM_EXIT_GRACE_S` = 1.0 с
   (при обычном конце гонки TORCS сначала шлёт `***shutdown***`, драйвер выходит сам, а TORCS завершается
   примерно тогда же — это не падение). Если за это время драйвер не вышел и в его логе нет
   `Server has stopped the race`, → статус `sim_crash`, драйвер останавливается тем же путём
   SIGINT → SIGKILL (CSV сбрасывается в `finally` драйвера).
2. **Обнаружение по логу.** Если в логе симулятора есть `Segmentation fault` — статус `sim_crash` в любом случае
   (поверх `timeout` / `no_connect` / `driver_error`).
3. **Балл за `sim_crash` — градуированный.** Если в CSV есть данные (≥ `MIN_ROWS` строк):
   `T + UNFINISHED_PENALTY·(1 − progress)·T + SIM_CRASH_PENALTY·T + штрафы`, где `T` — лимит времени заезда
   (как у `unfinished`), `штрафы = k_damage·урон + k_offtrack·секунды_вне_трассы`. Без данных — `FAIL_SCORE`.
   - Любой `sim_crash` хуже любого `finished`/`unfinished` при тех же штрафах: минимум crash = `2T` (progress = 1),
     максимум unfinished = `2T` (progress = 0), finished ≤ `T`. Равенство только в вырожденном случае
     «падение на финише» против «не сдвинулся с места».
   - Чем позже падение (больше progress), тем меньше балл — CMA-ES получает градиент «проехать дальше».
   - Если все круги уже пройдены, а симулятор упал после этого — всё равно `sim_crash` (на соревновании падение
     опасно в любой момент).
4. **`--k-damage` / `--k-offtrack`** — переопределяют `K_DAMAGE` / `K_OFFTRACK` (по умолчанию 0.01 / 1.0, как было).
   Действуют и в режиме `--score-csv`. В JSON результата в `constants` пишутся фактические значения
   (`K_DAMAGE`, `K_OFFTRACK`), плюс `SIM_CRASH_PENALTY`, `SIM_EXIT_GRACE_S`.
   С `train_cma.py`: `--fitness-args "--k-damage 0.05"` (строка разбирается `shlex.split` и дописывается
   в команду `fitness.py`; argparse принимает значение с `--` внутри, т.к. в нём есть пробел). Без пробела
   (`--fitness-args=--k-damage=0.05`) — тоже работает.
5. Строка по трассе печатает статус `sim_crash` (как и другие статусы); в JSON трассы — `sim_exit_code`.

`snakeoil3_gym.py` и `train_cma.py` не меняются.

### Константы
| Константа | Было | Стало | Зачем |
|-----------|------|-------|-------|
| `SIM_CRASH_PENALTY` | — | 1.0 (×T) | надбавка за падение симулятора, в долях лимита времени заезда |
| `SIM_EXIT_GRACE_S` | — | 1.0 с | сколько ждать драйвер после выхода симулятора, прежде чем считать это падением |
| `K_DAMAGE` | 0.01 | 0.01 (CLI `--k-damage`) | без изменений по умолчанию |
| `K_OFFTRACK` | 1.0 | 1.0 (CLI `--k-offtrack`) | без изменений по умолчанию |

Пример (T = 303 с, 1 круг без эталона): падение на progress 0.2 без штрафов → 303 + 0.8·303 + 303 = 848.4;
на progress 0.8 → 303 + 0.2·303 + 303 = 666.6; недоезд с progress 0 → 606.

## Как проверить
Локально (Windows, без TORCS):
```
python -m py_compile fitness.py
python fitness.py --help
python fitness.py --score-csv runs/0008_local_corkscrew.csv --laps 1
python fitness.py --score-csv runs/0008_local_corkscrew.csv --laps 1 --k-damage 0.05
```
плюс сквозной прогон с имитатором SCR-сервера через `--sim` (как в 0009), который обрывает поток и выходит.

На PERUN (внутри задачи SLURM, как в 0009; `<sif>` — образ, см. `hpc/README.md`):
```
apptainer exec --bind /mnt/scratch/$USER <sif> python3 fitness.py --params params/run3_best.json \
    --tracks road/e-track-1,road/corkscrew --keep-logs \
    --workdir /mnt/scratch/$USER/fitness_runs/0016 --out /mnt/scratch/$USER/fitness_runs/0016.json
```
Успех: `road/e-track-1` → `sim_crash` за ~2–3 с wall (а не 120 с), балл ≈ T + (1−progress)·T + T,
progress ≈ 785 / длина трассы, в `*.sim.log` — `Segmentation fault`; `road/corkscrew` → `finished` ≈ 76.4 с.
С `params/default.json` на e-track-1 → `finished` ≈ 94.4 с (ложного `sim_crash` при обычном конце гонки нет).

## План run4
- База: `--base params/run3_best.json`.
- Трассы обучения (15): `road/{corkscrew,aalborg,alpine-1,e-track-2,g-track-1,forza,wheel-1,ruudskogen,
  e-track-1,alpine-2,brondehach,e-track-3,g-track-2,ole-road-1,street-1}`.
- Отложенные (проверка, не в обучении): `road/{e-track-4,e-track-6,eroad,g-track-3,wheel-2}`.
- `road/spring` исключена: ≈22 км, один круг ≈5 мин симуляции — не помещается в лимит 300 с на круг.
- `--fitness-args "--k-damage 0.05"` (run3 best получал до 259 урона на незнакомой alpine-2).
- Для новых трасс нет эталона в `REFERENCE_LAP_TIME`, лимит круга — `--max-lap-time` (300 с).

## Результат
Офлайн (2026-11-27, Windows, Python 3.11):
- `py_compile` — OK; `--help` показывает `--k-damage [0.01]`, `--k-offtrack [1.0]`; `--k-damage -1` → ошибка, код 2.
- `--score-csv runs/0008_local_corkscrew.csv --score-csv runs/0006-baseline.csv --laps 1` — как в 0009:
  finished 113.33, unfinished 571.28, FITNESS 684.616.
  С `--k-damage 0.05 --k-offtrack 2`: 106.652 + 16·0.05 + 6.52·2 = 120.49 и 589.94 (FITNESS 710.436);
  в `--out` JSON `constants.K_DAMAGE = 0.05`, `K_OFFTRACK = 2.0`.
- `train_cma.py --fitness-args "--k-damage 0.05 --k-offtrack 1.5"` и `--fitness-args=--k-damage=0.05` —
  разбираются, команда `fitness.py` получает флаги, парсер `fitness.py` их принимает.
- Сквозной прогон с имитатором SCR-сервера через `--sim` (скрипт вне репо проигрывает
  `runs/0008_local_corkscrew.csv` по UDP и затем «падает»), corkscrew, T = 303 с, `--k-damage 0.05`:

| Сценарий имитатора | Статус | progress | Балл | От выхода имитатора до записи JSON |
|--------------------|--------|----------|------|------------------------------------|
| 1000 строк, строка `Segmentation fault`, выход 0 | sim_crash | 0.166 | 858.68 | 1.09 с |
| 3000 строк, `Segmentation fault`, выход 0 | sim_crash | 0.593 | 729.30 | 1.17 с |
| 1000 строк, тихий выход 0 (без строки в логе) | sim_crash | 0.166 | 858.68 | 1.09 с |
| весь круг, `***shutdown***`, выход сразу | finished | 1.000 | 113.97 | — |
| весь круг, `***shutdown***`, выход через 0.5 с | finished | 1.000 | 113.97 | 0.15 с |

  Позднее падение (729) лучше раннего (859), оба хуже недоезда без падения; ложного `sim_crash`
  при обычном конце гонки нет. `sim_exit_code` = 0 в JSON.
- Не проверено на Windows: SIGINT драйверу (там `terminate()`, CSV сбрасывается построчно, поэтому данные
  всё равно есть) и `killpg` групп процессов — только на Linux/PERUN.

Проверка на PERUN (2026-11-27, закоммиченный драйвер 0015 + этот `fitness.py`, порт 3001):

| Набор | Трасса | Статус | Балл | Wall |
|---|---|---|---|---|
| `params/run3_best.json`, `--k-damage 0.05` | e-track-1 | **sim_crash**, progress 0.247 | 834.03 | 3.4 с (было `timeout` 120 с, 10000) |
| `params/run3_best.json`, `--k-damage 0.05` | corkscrew | finished 76.43 с, урон 4 | 78.05 | 2.0 с |
| `params/default.json` | e-track-1 | finished 91.02 с — ложной тревоги нет | 92.31 | 2.0 с |

run4 (задача 177628, `--base params/run3_best.json --sigma0 0.15 --popsize 20`, 15 трасс обучения, `--fitness-args "--k-damage 0.05"`, код собран `git archive HEAD` + этот `fitness.py`):
- `port check passed: 10 ports, spread 0.568 <= tol 1` — впервые не 0.000. Вероятная причина — балл
  `sim_crash` на e-track-1 зависит от того, сколько строк CSV драйвер успел сбросить до остановки
  (`LOG_FLUSH_EVERY` = 50). На трассах без падения TORCS детерминирован. Шум 0.57 на фоне ≈2000 незначим.
- gen 0 (база run3 на 15 трассах) = **1997.06**; поколение ≈ 66 с; сбоев 0.
- Итог run4 — дописать после прогона.

## Заметки / открытые вопросы
- Убийство групп процессов (`killpg`) и SIGINT драйверу на Windows не проверяются (там `terminate()`,
  CSV драйвера при этом не сбрасывается в `finally`) — только на Linux.
- Точное место падения в C++ не найдено; если понадобится — запуск `torcs -d` (под gdb) на e-track-1.
- Падение на соревновании = 0 кругов; обучение с `sim_crash` должно уводить от опасной траектории,
  но гарантии нет — e-track-1 стоит проверить отдельно после run4.
