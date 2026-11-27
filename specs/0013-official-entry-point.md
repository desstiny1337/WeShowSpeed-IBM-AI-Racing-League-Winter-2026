# 0013 — torcs_jm_par.py runs the team driver

| Поле    | Значение                          |
|---------|-----------------------------------|
| Статус  | in-progress                       |
| Автор   | Hlib                              |
| Дата    | 2026-11-27                        |
| Файлы   | `torcs_jm_par.py`, `snakeoil3_gym.py`, `README.md` |
| Коммиты | (заполняет git-агент)             |

## Запрос / мотивация
Официальный Windows Setup Guide организаторов (`installation/windows` в `ibm-ai-racing/racing-league`)
называет `torcs_jm_par.py` главным стартовым файлом («Main starter file — edit this for the competition»)
и запускает машину командой `python torcs_jm_par.py`; `snakeoil3_gym.py` там помечен «do not edit».

У нас наоборот: драйвер команды — `drive_example()` в `snakeoil3_gym.py` (решение 2026-11-09),
а в `torcs_jm_par.py` оставалась отладочная заглушка `drive_modular` (`steer=1.0`, `accel=0.4`, `gear=1`) —
машина ездит по кругу. Если жюри запустит машину по своей инструкции, оно увидит заглушку.

## Что меняем
- `snakeoil3_gym.py`: основной цикл из `if __name__ == "__main__"` вынесен в функцию `main()` без
  изменений логики; `python snakeoil3_gym.py` работает как раньше.
- `torcs_jm_par.py`: удалены отладочная `drive_modular` и `print("!!! THIS FILE IS RUNNING !!!")` вверху файла; `__main__` вызывает `snakeoil3_gym.main()`.
  Флаги командной строки те же (`-p`, `--params`, `--log`, `-m`). Закомментированная исходная
  `drive_modular` и параметры стартового кита оставлены как есть (не используются).
- `README.md`: в шапке — как запустить нашу машину.

Параметры драйвера не меняются.

## Как проверить
1. `python -m py_compile torcs_jm_par.py snakeoil3_gym.py`.
2. TORCS → Practice → New Race (`scr_server`), затем `python torcs_jm_par.py` — машина должна ехать
   так же, как от `python snakeoil3_gym.py` (в консоли строка `params (defaults): ...`).
3. `python torcs_jm_par.py --log runs/entry_check.csv` — пишет телеметрию, как основной драйвер.

## Результат
(после заезда Hlib)

## Заметки / открытые вопросы
- Импорт `snakeoil3_gym` берётся из той же папки — запускать из корня репо (как в гайде организаторов).
