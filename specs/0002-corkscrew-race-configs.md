# 0002 — Конфиги гонки под Corkscrew (practice + quickrace)

| Поле    | Значение                                   |
|---------|--------------------------------------------|
| Статус  | done                                       |
| Автор   | Hlib                                       |
| Дата    | 2026-11-09                                 |
| Файлы   | `practice.xml`, `quickrace.xml`            |
| Коммиты | (заполняет git-агент)                      |

## Запрос / мотивация
Целевая трасса соревнования — Corkscrew. В репозитории `practice.xml` указывал на `michigan`
(овал), а у Hlib локально TORCS уже настроен на Corkscrew. Кроме того, для запусков без экрана
на HPC PERUN нужен отдельный конфиг: vtorcs в режиме `-T` игнорирует Practice и сам стартует
**Quick Race** из `<LocalDir>/config/raceman/quickrace.xml`
(см. `vtorcs-RL-color/src/libs/raceengineclient/raceinit.cpp`, `ReAddRacemanListButton`).

## Что меняем
1. `practice.xml` — заменён рабочей копией Hlib из `C:\torcs\torcs\config\raceman\practice.xml`.
2. `quickrace.xml` — новый, на основе `vtorcs-RL-color/exp-settings/config/raceman/quickrace.xml`
   (тот же код TORCS, что будет собран на PERUN).
### Параметры
| Параметр | Было | Стало | Почему |
|----------|------|-------|--------|
| practice: трасса | michigan (oval) | corkscrew (road) | целевая трасса |
| practice: Drivers Start List | inferno idx 5 | scr_server idx 0 | без бота, только наш драйвер |
| practice: focused module | scr_server | human | так сохранил GUI, на гонку не влияет |
| quickrace: трасса | — | corkscrew (road) | целевая трасса |
| quickrace: пилоты | — | только scr_server idx 0 | один драйвер на копию TORCS, без соперников |
| quickrace: круги | — | 2 | 1-й круг — с места, 2-й — «летящий»; для оценки особей достаточно, эволюция может переопределить |

## Как проверить
- Локально (Windows): ничего не меняется — Hlib продолжает настраивать Practice в GUI.
- Linux/PERUN: `torcs -T -c <dir> -p 3001`, где `<dir>/config/raceman/quickrace.xml` — этот файл;
  в логе должно быть `Starting Quick Race`, затем `Waiting for request on port 3001`.

## Результат
XML проверен парсером (`xml.etree`). Запуск `-T` ещё не проверен — нет собранного vtorcs.

## Заметки / открытые вопросы
- Дома — официальный TORCS + SCR-патч (`wtorcs.exe`), на PERUN — vtorcs. Физика должна совпадать,
  но лучшие параметры из эволюции нужно перепроверять дома обычным заездом.
- Нужно ли vtorcs в `-T` что-то ещё из LocalDir (screen.xml и т. п.) — выяснится при сборке.
