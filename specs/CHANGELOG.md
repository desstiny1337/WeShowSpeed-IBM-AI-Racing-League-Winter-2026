# Журнал изменений

Новые записи сверху. Формат: `дата | NNNN | автор | кратко | статус`.

| Дата       | Спека | Автор  | Кратко                                   | Статус |
|------------|-------|--------|------------------------------------------|--------|
| 2026-11-27 | [0013](0013-official-entry-point.md) | Hlib   | `python torcs_jm_par.py` (официальная точка входа) запускает наш драйвер через `snakeoil3_gym.main()`; убрана отладочная заглушка | in-progress |
| 2026-11-27 | [0012](0012-third-party-credits.md) | Hlib   | README: раздел «Third-party code and licenses» (п. 9.3 регламента) — откуда взят чужой код, лицензии, что написано командой | done |
| 2026-11-26 | [0011](0011-same-car-all-ports.md) | Hlib   | В образе порты 3002–3010 были на `car1-trb1`: `hpc/scr_server.xml` (все слоты `car1-ow1`) + bind в sbatch, проверка портов в gen 0 `train_cma.py` (код 3), шире границы поиска | done |
| 2026-11-26 | [0010](0010-cma-es-training.md) | Hlib   | `train_cma.py`: CMA-ES по 18 параметрам, 10 параллельных оценок `fitness.py`, контрольные точки; SLURM-задача и установка numpy/cma для PERUN | done |
| 2026-11-26 | [0009](0009-fitness-evaluator.md) | Hlib   | `fitness.py`: прогон параметров на 8 трассах (`torcs -r`), разбор CSV, один балл `FITNESS`; фикс `-p` в `snakeoil3_gym.py` | done |
| 2026-11-26 | [0008](0008-brake-and-params.md) | Hlib   | Торможение по дальномерам, ABS/TCS, передачи по rpm, выезд/задний ход; `DEFAULT_PARAMS` + `--params file.json` | done |
| 2026-11-16 | [0007](0007-branch-workflow.md) | Hlib   | Личные ветки dev/* и слияние в master через PR | done |
| 2026-11-09 | [0006](0006-telemetry-logger.md) | Hlib   | Логгер телеметрии `--log file.csv` + итоговая сводка; исходный заезд: 234 км/ч, вылет на 426 м без торможения | done |
| 2026-11-09 | [0005](0005-baseline-run-corkscrew.md) | Hlib   | Исходный заезд: без тормоза — вылет в повороте, круг не пройден | done |
| 2026-11-09 | [0004](0004-requirements.md) | Hlib   | requirements.txt (numpy, matplotlib), раздел «Окружение» | done |
| 2026-11-09 | [0003](0003-contributing-guide.md) | Hlib   | CONTRIBUTING.md — правила работы команды | done |
| 2026-11-09 | [0002](0002-corkscrew-race-configs.md) | Hlib   | practice.xml → Corkscrew, новый quickrace.xml для `-T` | done |
| 2026-11-08 | [0001](0001-baseline.md) | Hlib   | Снимок базового состояния проекта | done |
