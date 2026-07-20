# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/xvanov/software-factory-copy/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                                   |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------------------- | -------: | -------: | ------: | --------: |
| factory/\_\_init\_\_.py                                |        2 |        0 |    100% |           |
| factory/app\_config.py                                 |       69 |        4 |     94% |168, 172, 187, 205 |
| factory/artifacts/\_\_init\_\_.py                      |        0 |        0 |    100% |           |
| factory/backpressure/\_\_init\_\_.py                   |        0 |        0 |    100% |           |
| factory/backpressure/parser.py                         |       67 |        5 |     93% |103-104, 128, 132-133 |
| factory/backpressure/validator.py                      |       78 |        3 |     96% | 52-53, 72 |
| factory/chain/\_\_init\_\_.py                          |        0 |        0 |    100% |           |
| factory/chain/acceptance.py                            |      166 |       35 |     79% |88-89, 95-98, 148, 189, 191, 200-228, 236-237, 320, 350, 357-358, 371-372, 381-382, 384-385, 388-389 |
| factory/chain/auto\_merge.py                           |      407 |       76 |     81% |264-265, 312, 314, 316, 320-321, 548, 796, 830, 833-834, 836, 844, 863-864, 868-869, 887-888, 891, 959-960, 968-969, 1041-1042, 1124, 1149-1187, 1210, 1330-1331, 1354-1355, 1374-1378, 1399-1407, 1425-1429, 1458-1505, 1519-1520, 1539-1540 |
| factory/chain/branch.py                                |       56 |        4 |     93% |   163-166 |
| factory/chain/bug\_hunter.py                           |        8 |        8 |      0% |     13-62 |
| factory/chain/ci\_health.py                            |      197 |       28 |     86% |113-114, 131, 134, 178, 181, 201, 210, 217, 251, 264, 326, 330, 339, 343-344, 421-422, 430-431, 449-450, 468-469, 494-495, 505-506 |
| factory/chain/context\_refresh.py                      |      179 |       40 |     78% |123-130, 211, 222-248, 277-290, 360-369, 411, 442, 477, 524-526, 541-543, 545-548, 553-556, 564-565, 567, 571-572 |
| factory/chain/dual\_draft.py                           |      123 |       10 |     92% |63-64, 196, 227, 229, 233, 334, 364, 384-385 |
| factory/chain/ears.py                                  |       71 |        0 |    100% |           |
| factory/chain/event\_log.py                            |       55 |        3 |     95% |122, 132-133 |
| factory/chain/factory\_improver.py                     |      274 |       92 |     66% |142, 159-160, 162, 167-168, 184-221, 254-255, 262-263, 330, 373-395, 409-410, 435-436, 513-514, 535, 561, 629-667, 684-686, 700-712, 739-782 |
| factory/chain/factory\_improver\_apply.py              |      354 |       40 |     89% |120, 124, 204, 240, 253, 321, 386-387, 435-436, 446, 475-476, 523, 530-531, 554-555, 577, 614, 670-671, 699, 759, 792-801, 898-899, 902, 912-921, 935, 1008-1012 |
| factory/chain/factory\_status.py                       |      144 |        8 |     94% |81-82, 114-115, 117, 260, 282-284 |
| factory/chain/gates/\_\_init\_\_.py                    |        3 |        0 |    100% |           |
| factory/chain/gates/acceptance\_verified.py            |       39 |        2 |     95% |   138-139 |
| factory/chain/gates/canonical\_paths\_only.py          |       10 |        0 |    100% |           |
| factory/chain/gates/docs\_current.py                   |       23 |        3 |     87% | 19, 24-25 |
| factory/chain/gates/evaluator.py                       |       55 |        5 |     91% |128, 158-163 |
| factory/chain/gates/smoke\_green.py                    |       15 |        0 |    100% |           |
| factory/chain/gates/tests\_green.py                    |       38 |       10 |     74% |47-63, 122, 143 |
| factory/chain/gates/tests\_meaningful.py               |      100 |       17 |     83% |88, 103, 157, 160-161, 196, 216-217, 221, 240-249, 253 |
| factory/chain/handlers.py                              |     1223 |      217 |     82% |142-143, 172-173, 335-347, 415-429, 436-437, 465-466, 474-485, 507-508, 517-529, 548-549, 617-618, 622-623, 762-765, 767-770, 774-775, 817, 971, 1106, 1128-1130, 1144-1145, 1228, 1310-1316, 1360-1361, 1365-1367, 1529, 1551, 1558, 1566-1567, 1569-1572, 1631-1632, 1642-1649, 1651-1659, 1842-1844, 1896-1898, 1923-1924, 1951, 1981, 1997-2000, 2045-2046, 2054, 2060, 2112-2113, 2219, 2223-2224, 2276, 2289-2290, 2292-2303, 2358-2359, 2372-2373, 2375, 2389-2390, 2435-2436, 2438, 2448-2449, 2460-2461, 2473, 2536-2537, 2627-2631, 2721-2727, 2767-2768, 2782, 2833, 2867-2871, 2893-2896, 2997-2998, 3281-3285, 3358-3359, 3391-3395, 3415-3419, 3448-3483, 3512-3513, 3553-3561, 3603-3604, 3657-3658, 3660-3661, 3674-3675, 3679-3698, 3796-3797, 3919-3925 |
| factory/chain/idle.py                                  |      209 |       39 |     81% |91-92, 148-150, 158, 169, 172-173, 184-185, 248-250, 271-272, 283-297, 317-318, 340, 365, 368, 392-404, 422 |
| factory/chain/issue\_intake.py                         |       46 |        5 |     89% |55, 89-90, 92-93 |
| factory/chain/orchestrator.py                          |      537 |       89 |     83% |329, 344-348, 490, 705-706, 883-906, 941-942, 958-959, 1044-1054, 1151-1154, 1167, 1174-1194, 1214-1215, 1237-1239, 1246-1248, 1256-1258, 1276-1281, 1296-1297, 1319-1323, 1333-1334, 1369-1376, 1396-1397, 1428, 1513-1523, 1568-1569, 1699-1700, 1732-1736, 1751-1754, 1757-1759, 1779-1780 |
| factory/chain/pm\_sync.py                              |      249 |       40 |     84% |161-162, 164-165, 167-169, 187, 196, 425, 457, 498-502, 506, 545, 561-573, 589-599, 615, 622, 632-634, 657-658, 669, 681-683, 757-758, 760, 770-771, 781 |
| factory/chain/review\_events.py                        |       11 |        0 |    100% |           |
| factory/chain/rollback.py                              |      106 |        3 |     97% |102-103, 105 |
| factory/chain/scheduled\_tasks.py                      |      196 |       40 |     80% |231, 240-243, 344, 374-375, 407, 565-569, 584, 589, 598-600, 667-668, 689-726 |
| factory/chain/security.py                              |        7 |        7 |      0% |     11-46 |
| factory/chain/slop\_detector.py                        |      257 |       38 |     85% |109-112, 135, 137, 156, 160, 164, 179, 185, 195, 202, 236-237, 258-259, 289, 296, 300, 331-346, 361, 368, 370, 433, 491, 499-500, 503 |
| factory/chain/state\_machine.py                        |      112 |        0 |    100% |           |
| factory/chain/step\_events.py                          |       59 |        9 |     85% |111-112, 127-128, 157, 160-161, 168-169 |
| factory/chain/ux\_auditor.py                           |        7 |        7 |      0% |     14-49 |
| factory/chain/worktree.py                              |      112 |       25 |     78% |134-135, 140, 169, 172-174, 190-191, 232, 235-241, 262, 279-280, 303, 307, 316-317, 325, 327-328 |
| factory/cli.py                                         |     1307 |      720 |     45% |45-46, 61, 88-140, 160-171, 185-201, 209-215, 224-245, 262-295, 304-339, 348-360, 387-390, 414-419, 441-489, 493-494, 499, 523-558, 565-586, 603, 612-614, 623-630, 655-677, 695-740, 748-763, 787, 859, 872-873, 882, 888, 918-919, 921, 936-937, 939-940, 942, 967-975, 977, 995-996, 999-1001, 1003, 1021-1022, 1024, 1036-1074, 1103-1127, 1163-1164, 1262-1263, 1319-1323, 1363-1403, 1409-1414, 1438-1455, 1475-1549, 1553-1588, 1607-1617, 1639-1650, 1771-1803, 1815-1852, 1885, 1889-1897, 2015-2071, 2084-2111, 2129-2132, 2149, 2152, 2219-2274, 2314, 2348-2349, 2379-2385, 2441-2443, 2458-2481, 2504, 2507-2511, 2532-2539, 2543-2545, 2554-2555, 2561-2562, 2566, 2569-2570, 2575-2577, 2583-2584, 2694-2707, 2747-2761, 2805-2823, 2878-2903, 2922-2938, 2952-2970, 2992-3008, 3021-3045, 3085-3146 |
| factory/context/\_\_init\_\_.py                        |        0 |        0 |    100% |           |
| factory/context/canonical\_paths.py                    |       36 |        2 |     94% |     90-91 |
| factory/context/enforcer.py                            |       49 |        0 |    100% |           |
| factory/context/loader.py                              |       66 |        8 |     88% |96-110, 132 |
| factory/context/navigator.py                           |       39 |        2 |     95% |     65-66 |
| factory/context/updater.py                             |       29 |        1 |     97% |        77 |
| factory/deploy/\_\_init\_\_.py                         |        4 |        0 |    100% |           |
| factory/deploy/models.py                               |       28 |        0 |    100% |           |
| factory/deploy/orchestrator.py                         |      284 |       34 |     88% |257, 259-262, 265-267, 316, 402-405, 431-441, 486-490, 532-536, 555-556, 561, 578, 589, 596, 704-705 |
| factory/deploy/runner.py                               |       56 |        0 |    100% |           |
| factory/directions/\_\_init\_\_.py                     |        0 |        0 |    100% |           |
| factory/directions/creator.py                          |      150 |       69 |     54% |126, 156, 163, 197, 214-222, 232-343 |
| factory/directions/gc.py                               |       73 |       10 |     86% |59, 63, 76, 79-80, 82, 136-137, 165-166 |
| factory/directions/ingester.py                         |       77 |        2 |     97% |   55, 131 |
| factory/directions/parser.py                           |      242 |       21 |     91% |58, 148, 158-159, 173, 201-203, 258, 264-265, 269, 291, 297, 307, 333, 346, 358, 383, 396-398 |
| factory/directions/tracker\_issue.py                   |      151 |       17 |     89% |70-72, 110, 116, 120, 195-199, 256, 262, 270, 275, 281-282 |
| factory/directions/watcher.py                          |       76 |       25 |     67% |77-81, 86, 110-114, 122-132, 137-142 |
| factory/events/\_\_init\_\_.py                         |        1 |        0 |    100% |           |
| factory/events/rotation.py                             |       78 |       16 |     79% |62-63, 67-69, 96-98, 113, 116-117, 123-124, 144, 163-164 |
| factory/manager/\_\_init\_\_.py                        |        0 |        0 |    100% |           |
| factory/manager/apply.py                               |      460 |      174 |     62% |177-179, 193-195, 246-249, 288, 337, 342, 360, 363, 405-444, 456, 467-497, 506-563, 586, 599, 608, 658-665, 733-734, 755-761, 815-819, 829-832, 847-848, 853-855, 879-883, 913, 916-920, 924-928, 946-948, 958-960, 1001-1003, 1008, 1012-1017, 1092-1094, 1117, 1127, 1132-1133 |
| factory/manager/circuit\_breaker.py                    |      187 |       36 |     81% |126-127, 149, 154-157, 178-180, 195-197, 256-259, 267-272, 348-351, 372-376, 430-433, 437, 440, 467-468, 562-567 |
| factory/manager/detectors/\_\_init\_\_.py              |       15 |        0 |    100% |           |
| factory/manager/detectors/cost\_spike.py               |       50 |        4 |     92% |29, 32-33, 112 |
| factory/manager/detectors/placeholder\_prompts.py      |       32 |        1 |     97% |        66 |
| factory/manager/detectors/retry\_storm.py              |       44 |        3 |     93% | 67, 70-71 |
| factory/manager/detectors/review\_churn.py             |       52 |        5 |     90% |110, 113-114, 120, 123 |
| factory/manager/detectors/runs\_failed\_since.py       |       25 |        3 |     88% | 49, 52-53 |
| factory/manager/detectors/stalled\_stories.py          |      165 |       25 |     85% |50, 53-54, 56, 76, 79-80, 82, 86-87, 107, 110-111, 113, 117-118, 138, 141-142, 145-146, 166-167, 170, 273 |
| factory/manager/detectors/state\_distribution\_skew.py |       41 |        5 |     88% |77, 80-81, 83, 89 |
| factory/manager/detectors/tick\_duration\_outliers.py  |       70 |        8 |     89% |23, 25-26, 87, 90-91, 95, 117 |
| factory/manager/detectors/worktree\_orphans.py         |       38 |        4 |     89% |64-65, 86-87 |
| factory/manager/diagnostician.py                       |      466 |      101 |     78% |235-236, 248-249, 348-349, 356, 363, 402-403, 405, 537-539, 548, 559-560, 565, 570-572, 575, 583, 642-646, 727-728, 742, 783-786, 815-820, 869-870, 896-897, 942-944, 963-964, 1026-1031, 1033-1038, 1145-1146, 1158-1159, 1180-1181, 1213-1301 |
| factory/manager/escalation.py                          |      177 |       21 |     88% |126-128, 138-139, 152-153, 167, 289-290, 292, 296, 346, 409-410, 455-456, 493-494, 531-532 |
| factory/manager/halt.py                                |      126 |       24 |     81% |101-102, 180-185, 247, 293-296, 314-315, 317, 322-324, 332-337 |
| factory/manager/recovery.py                            |      424 |       64 |     85% |181, 184-185, 187, 227-231, 244, 247, 249, 285, 294-295, 352, 363-364, 401, 410, 415-416, 470, 483-484, 496, 498, 552-553, 564-565, 613, 630-631, 663, 669, 672-673, 676, 810, 820-821, 828, 837-838, 857, 860-861, 864-865, 881, 902, 904, 934-935, 1005-1006, 1213-1232 |
| factory/manager/self\_context.py                       |      141 |       29 |     79% |43-45, 55-57, 170-171, 189, 200, 203, 206-207, 212-213, 257-258, 292-298, 323, 338-339, 388, 393-400 |
| factory/manager/signals.py                             |       84 |        3 |     96% |125-126, 290 |
| factory/manager/staging.py                             |      152 |       11 |     93% |206, 307-308, 394, 437, 452, 470, 562-583 |
| factory/manager/summarizer.py                          |      414 |      104 |     75% |42-44, 55-57, 118, 123, 147, 150-151, 155-156, 180, 183-184, 204, 207-208, 212-213, 215, 219, 221-222, 293, 305, 308-309, 311, 332-333, 573-576, 578, 584-606, 647-649, 670-671, 674-675, 776-778, 783-785, 883-963 |
| factory/manager/watcher.py                             |      371 |      113 |     70% |41-43, 54-56, 107, 110-111, 115-116, 131, 146, 149-150, 152-153, 164, 168, 170-171, 235-246, 379-382, 384, 392-414, 482-483, 490-491, 501-502, 507-508, 515-516, 523-524, 529-530, 539-540, 552-553, 618-619, 622-625, 741-768, 777-783, 800-803, 835, 840, 868-869, 871, 892, 932-945, 956-989 |
| factory/model\_router.py                               |      122 |        9 |     93% |53, 55, 67, 89, 100, 189-192 |
| factory/observability/\_\_init\_\_.py                  |        0 |        0 |    100% |           |
| factory/observability/estimator.py                     |      185 |       31 |     83% |170-185, 219, 242-244, 331, 333, 337, 384, 417, 450, 470, 489, 491, 493, 498 |
| factory/observability/heartbeat.py                     |       60 |        7 |     88% |69, 74-77, 129-130 |
| factory/observability/queries.py                       |      321 |       62 |     81% |150-151, 153, 197-200, 253-258, 265, 317-318, 332, 360-361, 455, 457-481, 529-532, 538, 586-589, 635-644, 647-653 |
| factory/observability/schema.py                        |       48 |        0 |    100% |           |
| factory/personas/\_\_init\_\_.py                       |        0 |        0 |    100% |           |
| factory/providers/\_\_init\_\_.py                      |        0 |        0 |    100% |           |
| factory/providers/azure\_foundry.py                    |       34 |        0 |    100% |           |
| factory/providers/github.py                            |       23 |        5 |     78% |     76-81 |
| factory/runner.py                                      |      679 |      106 |     84% |132-133, 152, 159-160, 341-342, 368-369, 380, 422, 456-457, 459-464, 483-484, 487-493, 554-555, 594-595, 611, 614, 616, 621-622, 630-645, 653, 656-662, 671-681, 686, 689, 728, 743, 780-781, 786-798, 880, 882-886, 1169, 1171, 1215-1238, 1284-1289, 1310, 1639, 1666-1667, 1675-1679, 1748, 1762-1763, 1766-1767 |
| factory/runtime\_state.py                              |       51 |        0 |    100% |           |
| factory/scheduler/\_\_init\_\_.py                      |        0 |        0 |    100% |           |
| factory/scheduler/cron.py                              |      132 |       11 |     92% |117, 122, 124, 171-175, 211, 353-354 |
| factory/settings/\_\_init\_\_.py                       |        0 |        0 |    100% |           |
| factory/settings/audit.py                              |      113 |        5 |     96% |108-109, 111, 140-141 |
| factory/settings/enforcer.py                           |       54 |        0 |    100% |           |
| factory/settings/loader.py                             |       89 |        0 |    100% |           |
| factory/settings/modes.py                              |       40 |        1 |     98% |        67 |
| factory/settings/spend.py                              |       83 |       24 |     71% |56-57, 78-79, 93, 120-133, 152-158 |
| factory/testing/\_\_init\_\_.py                        |        0 |        0 |    100% |           |
| factory/testing/flake.py                               |      124 |       24 |     81% |91, 121-149, 236-237, 239, 255, 282-283, 335-336, 339 |
| factory/tui/\_\_init\_\_.py                            |        2 |        0 |    100% |           |
| factory/tui/app.py                                     |      183 |      151 |     17% |48-57, 61, 66-74, 78, 87-121, 130-161, 166-232, 241-250, 254-274, 278-312, 316-339, 372-377, 380-388, 391-406, 409-415, 418-442, 454-461 |
| factory/webhook/\_\_init\_\_.py                        |        0 |        0 |    100% |           |
| factory/webhook/github.py                              |      190 |       40 |     79% |56, 60, 63-64, 96, 126, 162-167, 191-207, 253, 308-312, 355-356, 379-391, 395 |
| factory/webhook/openhands\_events.py                   |       36 |        7 |     81% |69, 84-123 |
| **TOTAL**                                              | **14513** | **3060** | **79%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/xvanov/software-factory-copy/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/xvanov/software-factory-copy/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/xvanov/software-factory-copy/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/xvanov/software-factory-copy/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fxvanov%2Fsoftware-factory-copy%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/xvanov/software-factory-copy/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.