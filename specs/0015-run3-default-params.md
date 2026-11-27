# 0015 — CMA-ES run3 parameters as driver defaults

| Поле    | Значение                          |
|---------|-----------------------------------|
| Статус  | in-progress                       |
| Автор   | Hlib                              |
| Дата    | 2026-11-27                        |
| Файлы   | `snakeoil3_gym.py`, `params/run3_best.json` |
| Коммиты | (заполняет git-агент)             |

## Запрос / мотивация
Организаторы запускают машину командой `python torcs_jm_par.py` (спека 0013) — **без `--params`**.
Значит, в зачёт идут `DEFAULT_PARAMS` из `snakeoil3_gym.py`, а это ручные параметры спеки 0008:
Corkscrew за 106.65 с с тремя выездами.

Лучший набор обучения CMA-ES (спеки 0010/0011) — `cma_runs/run3/best.json` на PERUN (поколение 1337,
FITNESS 650.874 против 976.602 у `params/default.json`) — проходит Corkscrew за 76.43 с. В репо его не было.

Сдача — 30.11.2026 (дедлайн не подтверждён, см. память о регламенте), поэтому в версию к сдаче
берём лучшее, что есть, и фиксируем известные риски.

## Что меняем
- `params/run3_best.json` — `best.json` из run3 как есть (32 ключа, те же, что в `DEFAULT_PARAMS`).
- `snakeoil3_gym.py`: значения `DEFAULT_PARAMS` = `params/run3_best.json` без округления
  (TORCS детерминирован — так круг повторяется до сотых). Комментарии и порядок ключей не менялись;
  целочисленные параметры остались `int` (`train_cma.py` определяет тип по `DEFAULT_PARAMS`).
- `params/default.json` **не меняется** — это ручная базовая линия 0008 и опорная точка обучения
  (FITNESS 976.6, `--base` в `train_cma.py`). Инвариант «`default.json` = `DEFAULT_PARAMS`» из 0008 больше
  не действует — это записано в комментарии над `DEFAULT_PARAMS`.

### Параметры
| Параметр | Было (0008) | Стало (run3) |
|----------|------|-------|
| v_corner | 80.0 | 54.36 |
| decel | 12.0 | 28.36 |
| dist_margin | 15.0 | 15.50 |
| ahead_halfwidth | 2 | 5 |
| curv_speed_gain | 0.0 | 0.000523 |
| accel_gain | 0.1 | 0.1615 |
| brake_gain | 0.1 | 0.1921 |
| abs_slip / abs_gain | 0.15 / 3.0 | 0.1013 / 4.760 |
| tcs_slip / tcs_gain | 0.15 / 3.0 | 0.1017 / 3.605 |
| steer_angle_gain | 4.775 | 1.508 |
| steer_pos_gain | 0.10 | 0.0691 |
| steer_ray_gain | 0.0 | 2.992 |
| gear_up_rpm / gear_down_rpm | 17500 / 11000 | 18600 / 11583 |
| offtrack_speed | 50.0 | 85.72 |
| offtrack_pos_gain | 0.5 | 0.428 |

Остальные (`v_max`, `v_min`, `brake_hyst`, радиусы колёс, `*_min_speed`, `gear_max`, stuck/reverse) — без изменений.
В таблице значения округлены; точные — в `params/run3_best.json`.

## Как проверить
1. `python -m py_compile snakeoil3_gym.py torcs_jm_par.py`; `DEFAULT_PARAMS == json.load(params/run3_best.json)`.
2. TORCS → Practice → New Race (Corkscrew, `scr_server`), затем
   `python torcs_jm_par.py --log runs/0015_corkscrew.csv` (без `--params`) — круг ≈ 76.4 с,
   в консоли `params (defaults): ...` со значениями run3.

## Результат
Данные run3 на PERUN (`best_result.json`, 1 круг, `car1-ow1`, без соперников):

| Трасса | Круг, с | Урон | Вне трассы, с |
|--------|---------|------|---------------|
| corkscrew | 76.434 | 4 | 1.42 |
| aalborg | 70.536 | 0 | 0 |
| alpine-1 | 125.200 | 0 | 0 |
| e-track-2 | 111.754 | 0 | 0 |
| g-track-1 | 37.560 | 0 | 0 |
| forza | 86.984 | 0 | 0 |
| wheel-1 | 80.094 | 0 | 0 |
| ruudskogen | 60.852 | 0 | 0 |

На 13 трассах вне обучения (2026-11-27): быстрее 0008 на 11 трассах, в сумме на 31% (1319.8 → 907.1 с).
Локальный заезд через `torcs_jm_par.py` без `--params` — (после заезда Hlib).

## Заметки / открытые вопросы
- **e-track-1: TORCS падает (segfault) примерно на 785 м.** Детерминированно, зависит от траектории:
  `steer_ray_gain` = 2.99 ведёт машину по левому краю после поворота. Если эта трасса выпадет на
  квалификации, будет 0 кругов. Без ray круг 94.4 с. Разобрать в следующем обучении (run4): ловить
  падение симулятора в `fitness.py`, добавить e-track-1 и другие трассы в обучение.
- Урон выше, чем у 0008, на alpine-2 (259) и ole-road-1 (139).
- Обучение было без соперников; поведение в масс-старте не проверялось.
