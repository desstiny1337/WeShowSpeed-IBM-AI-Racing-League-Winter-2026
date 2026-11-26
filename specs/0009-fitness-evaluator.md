# 0009 — Фитнес-функция: оценка набора параметров на нескольких трассах

| Поле    | Значение                          |
|---------|-----------------------------------|
| Статус  | done                              |
| Автор   | Hlib                              |
| Дата    | 2026-11-26                        |
| Файлы   | `fitness.py` (новый), `snakeoil3_gym.py` (одна строка в `__main__`), `.gitignore` |
| Коммиты | e75d93f                           |

## Запрос / мотивация
После 0008 все настраиваемые числа драйвера вынесены в `--params file.json`. Следующий шаг —
подбирать их CMA-ES на SLURM-кластере (PERUN). Для этого нужен скрипт, который берёт один файл
параметров, прогоняет драйвер на наборе трасс (TORCS 1.3.7 в режиме `torcs -r`, без графики) и
выдаёт одно число — фитнес (меньше = лучше). Только стандартная библиотека Python (на кластере —
Python 3.10 в образе apptainer, локально — 3.11 на Windows). Сбой отдельной трассы не должен ронять
оценку: он отражается в баллах.

## Что меняем

### `fitness.py` (новый файл, только stdlib)
Константы вверху файла: `DEFAULT_TRACKS` (8 трасс из 0008, все `road`), `REFERENCE_LAP_TIME`
(времена 0008), коэффициенты оценки и параметры запуска — у каждой комментарий.

Для каждой трассы:
1. Из шаблона `quickrace.xml` текстовой заменой (regex, DOCTYPE и всё остальное как есть) создаётся
   `<workdir>/race_<cat>_<name>.xml`: `name`/`category` в секции `Tracks/1`, `laps`,
   `idx`/`focused idx` драйвера = `port − 3001` (scr_server слушает порт `3001 + idx`, idx 0..9).
2. Запуск симулятора: `shlex.split(--sim) + ['torcs', '-r', <xml>]`, cwd = workdir, вывод в
   `<tag>.sim.log`, `start_new_session=True` (своя группа процессов). Все пути абсолютные — контейнер
   должен видеть workdir (через `--bind`).
3. Через `--sim-start-wait` (1.5 с) — драйвер: `<python> -u snakeoil3_gym.py --params <abs> --log <csv>
   -m <steps> -p <port>`, вывод в `<tag>.driver.log` (`-u` — чтобы вывод шёл в лог сразу).
   - Лимит симуляции в шагах: `steps = ceil((laps · T_lap + START_MARGIN_S) · 50)`,
     `T_lap = min(--max-lap-time, --time-limit-factor · reference)` для известных трасс, иначе `--max-lap-time`.
   - Контроль: если в логе драйвера за `--connect-timeout` (5 с) нет `Client connected` → `no_connect`.
     Важно держать < 7 с: после ~7 неудачных попыток `snakeoil3_gym.py` сам делает `pkill torcs`
     (старый код клиента), что на общем узле убило бы соседние оценки.
   - Если драйвер работает дольше `--wall-timeout` (120 с реального времени) → `timeout`.
   - Ненулевой код выхода драйвера → `driver_error`; симулятор не стартовал / упал до драйвера → `sim_error`.
4. Остановка: драйверу SIGINT (его `finally` дописывает CSV), через 3 с — SIGKILL всей группе;
   симулятору SIGTERM, через 3 с — SIGKILL группе (добивает `torcs-bin`, оболочку, детей apptainer).
   На SIGTERM от SLURM сам `fitness.py` выходит через `SystemExit`, и `finally` всё равно гасит процессы.
5. Разбор CSV, оценка, строка в stdout. Без `--keep-logs` xml/CSV/логи трассы удаляются.

В конце: `FITNESS <value>` (последняя строка) и JSON в `--out` (по умолчанию `<workdir>/result.json`):
путь и действующие значения параметров (через `snakeoil3_gym.load_params`, он же валидирует файл до
запуска), трассы, круги, порт, константы, метрики каждой трассы, итог, время. Код выхода 0 даже при
сбое трасс, 2 — ошибки использования (нет `--params`, плохой JSON, порт вне 3001..3010, нет шаблона).

Офлайн-режим `--score-csv <csv>` (повторяемый): оценить готовые CSV без симулятора
(лимит = `laps · --max-lap-time + START_MARGIN_S`, фолбэк последнего круга выключен).

### Разбор CSV
Колонки логгера 0006: `step, curLapTime, lastLapTime, distFromStart, distRaced, speedX, speedY,
trackPos, angle, track_ahead, track_min, rpm, gear, damage, steer, accel, brake, gear_cmd`.
Нечитаемые строки (обрезанная последняя) пропускаются; < 10 строк = нет данных.
- **Круги:** как в логгере — круг завершён, когда `lastLapTime` меняется на новое положительное значение.
- **Фолбэк последнего круга:** TORCS может закончить гонку (и прислать `***shutdown***`) в том же шаге,
  где машина пересекла линию, тогда новое `lastLapTime` не попадает в CSV. Если в логе драйвера есть
  `Server has stopped the race`, не хватает ровно одного круга и прогресс ≥ `FINISH_PROGRESS_MIN`,
  последний круг = `curLapTime` последней строки + 0.02 с (флаг `last_lap_estimated`).
- **Длина трассы L** (заранее неизвестна). `distRaced` в scr_server — накопленный путь от места
  старта (на старте 0), `distFromStart` — от линии; машина стоит позади линии (`distFromStart` ≈ L − отступ).
  При первом пересечении линии вперёд (`distFromStart` падает > 100 м, `distRaced` растёт):
  `L = dR_cur − dR_prev + dfs_prev − dfs_cur` (точно). Если пересечения не было — `L ≈ max(distFromStart)`
  (ошибка ≤ отступа сетки, 10–25 м). Проверка на `0008_local_corkscrew.csv`: L = 3608.45 м, отступ 10 м.
- **Прогресс:** `need = (L − dfs0) + laps·L` (доехать до линии + `laps` кругов), `progress = clip(distRaced_last / need, 0, 1)`.
- **Урон** — `damage` последней строки; **вне трассы** — шаги с `|trackPos| > 1` / 50 (с) и число выездов;
  также `distRaced`, макс. `speedX`.

### Формула (минимизируем, единицы — секунды)
- penalty = `K_DAMAGE · damage + K_OFFTRACK · offtrack_s`
- финишировал (`laps_done ≥ laps`): `score = Σ lap_times + penalty`
- не финишировал: `score = T + UNFINISHED_PENALTY · (1 − progress) · T + penalty`, где
  `T = steps / 50` — фактический лимит симуляции (≥ любого финишного времени, поэтому любой финиш
  лучше любого нефиниша с теми же штрафами)
- сбой (`no_data`, `timeout`, `driver_error`, `no_connect`, `sim_error`): `score = FAIL_SCORE`
- итог: `FITNESS = Σ score` по трассам

### Константы
Новых параметров драйвера нет; это параметры оценки (в `fitness.py`).

| Константа | Значение | Почему |
|-----------|----------|--------|
| `K_DAMAGE` | 0.01 с/ед. | 1000 урона = 10 с (~10 % круга). Урон на соревновании копится (10000 — машина выбывает) и в гонке с соперниками опасен; aalborg 0008 (урон 1204) получит +12 с — заметно, но не перевешивает разницу во времени между «быстро» и «очень медленно» |
| `K_OFFTRACK` | 1.0 с/с | Вылет и так стоит времени (вне трассы `offtrack_speed`=50 км/ч); дополнительная секунда за секунду — против срезок и езды по обочине, которые на других трассах кончаются стеной |
| `UNFINISHED_PENALTY` | 1.0 | Нефиниш с прогрессом p стоит `T·(2 − p)`: от T (почти доехал) до 2T (встал на старте). Есть градиент «дальше = лучше», и любой нефиниш хуже любого финиша |
| `FAIL_SCORE` | 10000 с | Заведомо больше максимума нефиниша (~2·300 + 300 вне трассы + ~100 урона ≈ 1000) — сбой всегда хуже; для ранговой CMA-ES это просто «худший» |
| `FINISH_PROGRESS_MIN` | 0.995 | Фолбэк круга только если машина реально у линии (≤ ~18 м на Corkscrew) |
| `START_MARGIN_S` | 3.0 с | Отсчёт до старта (~1 с, `curLapTime < 0`) + запас |
| `--max-lap-time` | 300 с | Лимит на круг; медленный эталон alpine-1 — 189 с |
| `--time-limit-factor` | 3.0 | Для известных трасс лимит `min(300, 3·эталон)`: g-track-1 — 168 с вместо 300, застрявшие кандидаты дешевле |
| `--sim-start-wait` | 1.5 с | Так работало вручную на кластере |
| `--connect-timeout` | 5 с | < 7 с до `pkill torcs` в клиенте |
| `--wall-timeout` | 120 с | Круг в `torcs -r` занимает 0.4–1 с реального времени (0008) — с большим запасом |

### `snakeoil3_gym.py` — одна строка (необходимо)
В `__main__` было `C= Client(p=3001)`. Конструктор применяет `p` **после** разбора командной строки,
поэтому `-p/--port` молча игнорировался и драйвер всегда шёл на 3001. Для `fitness.py --port`
(несколько оценок на одном узле на разных портах) это обязательно. Стало `C= Client()` — порт по
умолчанию в `Client` и так 3001, `-p` теперь работает. `gym_torcs.py` создаёт `Client(p=3101)` явно —
не затронут. Копии клиента (`jmcncarai.py`, `snakeoil3_jm2.py`, `torcs_jm_par.py`) не трогали — у них та же
особенность, это эксперименты.

### `.gitignore`
`fitness_runs/` — рабочая папка по умолчанию.

## CLI
```
python3 fitness.py --params P.json [--tracks road/corkscrew,road/aalborg,...] [--laps 1]
    [--sim "<prefix>"] [--python "<cmd>"] [--driver snakeoil3_gym.py] [--race-template quickrace.xml]
    [--workdir DIR] [--out result.json] [--port 3001] [--max-lap-time 300] [--time-limit-factor 3]
    [--sim-start-wait 1.5] [--connect-timeout 5] [--wall-timeout 120] [--keep-logs]
python3 fitness.py --score-csv run.csv [--score-csv run2.csv] [--laps 1] [--out r.json]
```
Трасса — `category/name` (без категории — `road`). `--python` — чем запускать драйвер (по умолчанию
тот же интерпретатор, что и `fitness.py`).

## Как проверить
Офлайн (сделано, Windows, Python 3.11):
```
python -m py_compile fitness.py snakeoil3_gym.py
python fitness.py --help
python fitness.py --score-csv runs/0008_local_corkscrew.csv --score-csv runs/0006-baseline.csv --laps 1
```

На PERUN (внутри задачи SLURM; `$W` — рабочая папка на scratch, HOME должен быть доступен на запись —
TORCS создаёт `~/.torcs` при первом запуске):
```
cd /mnt/scratch/$USER/gym_torcs
python3 fitness.py --params params/default.json \
    --sim "apptainer exec --bind /mnt/scratch/$USER /mnt/project/$PERUN_PROJECT/torcs/torcs-env.sif" \
    --workdir /mnt/scratch/$USER/fitness_runs/default --keep-logs \
    --out /mnt/scratch/$USER/fitness_runs/default.json
```
(`$PERUN_PROJECT` — номер проекта, см. `hpc/README.md`.) Если на хосте нет `python3`, запускать всё внутри образа:
`apptainer exec --bind /mnt/scratch/$USER <sif> python3 fitness.py --params params/default.json --keep-logs`
(тогда `--sim` пустой). Несколько оценок на одном узле — разные `--port` (3001..3010) и разные `--workdir`.

Успех: 8 строк со статусом `finished`, времена близки к 0008 (corkscrew 104.6, aalborg 97.3, alpine-1 189.1,
e-track-2 161.9, g-track-1 55.9, forza 120.3, wheel-1 117.0, ruudskogen 91.2), урон 0 кроме aalborg ~1204;
ожидаемый FITNESS ≈ 937 + 12 (урон aalborg) + выезды. Проверить, были ли `last_lap_estimated: true`
в JSON (т.е. пришло ли финальное `lastLapTime` от TORCS 1.3.7).

## Результат
Офлайн-проверка (2026-11-26, Windows, Python 3.11):
- `py_compile` — OK; `--help` — OK; без `--params`, неизвестный ключ в JSON, `--port 3020` → сообщение и код 2.
- `--score-csv`, `--laps 1`:

| CSV | Статус | Круг, с | Урон | Вне трассы | Прогресс | Балл |
|-----|--------|---------|------|------------|----------|------|
| `runs/0008_local_corkscrew.csv` | finished | 106.652 | 16 | 326 шагов = 6.52 с (3 выезда) | 1.000 | 106.652 + 0.16 + 6.52 = **113.332** |
| `runs/0006-baseline.csv` | unfinished | — | 395 | 143 шага = 2.86 с | 495.9 / 3618.4 = 0.137 | 303 + 0.863·303 + 3.95 + 2.86 = **571.28** |

  L по пересечению линии в обоих — 3608.45 м.
- Сквозной прогон с имитатором SCR-сервера (скрипт, проигрывающий `0008_local_corkscrew.csv` по UDP,
  вместо `torcs` через `--sim`): `--port 3003` (драйвер реально подключился к 3003 — исправление `-p`
  работает; idx в xml = 2) → finished, 113.332; обрыв за шаг до линии + `***shutdown***` → фолбэк,
  круг 106.664 (реальный 106.652), 113.344; обрыв на 2000 шаге → unfinished, 489.64; зависание сервера →
  `timeout` по `--wall-timeout`, 10000, частичные метрики в JSON; сервер не слушает → `no_connect` за 5 с
  (до `pkill` в клиенте), 10000; симулятор упал / не найден → `sim_error`, 10000. Код выхода везде 0.
  Убийство групп процессов (`killpg`) на Windows не проверялось — только на Linux.

Заезд на HPC (2026-11-26, PERUN, `cpu_short`, 4 ядра; `fitness.py` запущен внутри образа:
`apptainer exec --bind /mnt/scratch/$USER torcs-env.sif python3 fitness.py --params params/default.json
--keep-logs ...`, `--sim` пустой, `torcs` из образа), 1 круг, 8 трасс по умолчанию:

| Трасса | Круг, с | Урон | Вне трассы, с | Балл |
|---|---|---|---|---|
| corkscrew  | 104.59 | 0    | 4.66  | 109.25 |
| aalborg    | 97.30  | 1204 | 17.22 | 126.56 |
| alpine-1   | 189.11 | 0    | 0     | 189.11 |
| e-track-2  | 161.85 | 0    | 5.38  | 167.23 |
| g-track-1  | 55.92  | 0    | 0     | 55.92  |
| forza      | 120.29 | 0    | 0     | 120.29 |
| wheel-1    | 116.99 | 0    | 0     | 116.99 |
| ruudskogen | 91.25  | 0    | 0     | 91.25  |

**FITNESS 976.602** — опорное значение для `params/default.json`. Время кругов совпадает с ручными
прогонами 0008 до сотых. На всех трассах `last_lap_estimated = false` (финальный `lastLapTime` доходит
до CSV, фолбэк не понадобился), длина трассы везде определена точно (`wrap`). Реальное время всей оценки
— 17.3 с (≈2.1 с на трассу, из них 1.5 с — ожидание старта TORCS).

## Заметки / открытые вопросы
- Сумма секунд по трассам даёт длинным трассам больший вес; если понадобится — нормировать на эталон.
- Если на TORCS 1.3.7 финальное `lastLapTime` не приходит, все финиши будут через фолбэк (точность ~0.02 с).
- Параллельные оценки на одном узле делят `~/.torcs` (результаты гонок TORCS пишет туда); на оценку не влияет.
- Соперники и квалификация/гонка — позже: сейчас одна машина, `quickrace.xml` как шаблон.
