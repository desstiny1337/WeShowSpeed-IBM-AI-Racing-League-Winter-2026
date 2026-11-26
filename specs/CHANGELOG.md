# Журнал изменений

Новые записи сверху. Формат: `дата | NNNN | автор | кратко | статус`.

| Дата       | Спека | Автор  | Кратко                                   | Статус |
|------------|-------|--------|------------------------------------------|--------|
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
