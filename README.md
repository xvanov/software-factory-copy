# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/xvanov/software-factory-copy/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                                   |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------------------- | -------: | -------: | ------: | --------: |
| factory/\_\_init\_\_.py                                |        2 |        0 |    100% |           |
| factory/app\_config.py                                 |       79 |        5 |     94% |168, 172, 187, 205, 221 |
| factory/artifacts/\_\_init\_\_.py                      |        0 |        0 |    100% |           |
| factory/backpressure/\_\_init\_\_.py                   |        0 |        0 |    100% |           |
| factory/backpressure/parser.py                         |       67 |        5 |     93% |103-104, 128, 132-133 |
| factory/backpressure/validator.py                      |       78 |        3 |     96% | 52-53, 72 |
| factory/chain/\_\_init\_\_.py                          |        0 |        0 |    100% |           |
| factory/chain/acceptance.py                            |      166 |       35 |     79% |88-89, 95-98, 148, 189, 191, 200-228, 236-237, 320, 350, 357-358, 371-372, 381-382, 384-385, 388-389 |
| factory/chain/auto\_merge.py                           |      496 |       90 |     82% |281-282, 329, 331, 333, 337-338, 503-504, 506, 543, 562, 658-659, 747, 819, 848-849, 1082, 1116, 1119-1120, 1122, 1130, 1149-1150, 1154-1155, 1173-1174, 1177, 1245-1246, 1254-1255, 1327-1328, 1412, 1437-1475, 1498, 1621-1622, 1645-1646, 1665-1669, 1690-1698, 1716-1720, 1749-1796, 1818-1819, 1830-1831, 1854-1855, 1874-1875 |
| factory/chain/branch.py                                |       56 |        4 |     93% |   163-166 |
| factory/chain/bug\_hunter.py                           |        8 |        8 |      0% |     13-62 |
| factory/chain/ci\_health.py                            |      197 |       28 |     86% |113-114, 131, 134, 178, 181, 201, 210, 217, 251, 264, 326, 330, 339, 343-344, 421-422, 430-431, 449-450, 468-469, 494-495, 505-506 |
| factory/chain/context\_refresh.py                      |      179 |       40 |     78% |123-130, 211, 222-248, 277-290, 360-369, 411, 442, 477, 524-526, 541-543, 545-548, 553-556, 564-565, 567, 571-572 |
| factory/chain/dual\_draft.py                           |      145 |       13 |     91% |64-65, 197, 228, 230, 234, 374, 423-424, 453-454, 456-457 |
| factory/chain/ears.py                                  |       71 |        0 |    100% |           |
| factory/chain/event\_log.py                            |       55 |        3 |     95% |122, 132-133 |
| factory/chain/factory\_improver.py                     |      274 |       92 |     66% |142, 159-160, 162, 167-168, 184-221, 254-255, 262-263, 330, 373-395, 409-410, 435-436, 513-514, 535, 561, 629-667, 684-686, 700-712, 739-782 |
| factory/chain/factory\_improver\_apply.py              |      354 |       40 |     89% |120, 124, 204, 240, 253, 321, 386-387, 435-436, 446, 475-476, 523, 530-531, 554-555, 577, 614, 670-671, 699, 759, 792-801, 898-899, 902, 912-921, 935, 1008-1012 |
| factory/chain/factory\_status.py                       |      144 |        8 |     94% |83-84, 116-117, 119, 262, 284-286 |
| factory/chain/gates/\_\_init\_\_.py                    |        3 |        0 |    100% |           |
| factory/chain/gates/acceptance\_verified.py            |       39 |        2 |     95% |   138-139 |
| factory/chain/gates/canonical\_paths\_only.py          |       10 |        0 |    100% |           |
| factory/chain/gates/docs\_current.py                   |       23 |        3 |     87% | 19, 24-25 |
| factory/chain/gates/evaluator.py                       |       55 |        5 |     91% |128, 158-163 |
| factory/chain/gates/smoke\_green.py                    |       15 |        0 |    100% |           |
| factory/chain/gates/tests\_green.py                    |       38 |       10 |     74% |47-63, 122, 143 |
| factory/chain/gates/tests\_meaningful.py               |      100 |       17 |     83% |88, 103, 157, 160-161, 196, 216-217, 221, 240-249, 253 |
| factory/chain/handlers.py                              |     1268 |      228 |     82% |142-143, 172-173, 338-350, 418-432, 439-440, 468-469, 477-488, 510-511, 520-532, 551-552, 620-621, 625-626, 765-768, 770-773, 777-778, 820, 974, 1109, 1131-1133, 1147-1148, 1231, 1313-1319, 1355-1356, 1387-1388, 1392-1394, 1560, 1582, 1589, 1597-1598, 1600-1603, 1662-1663, 1673-1680, 1682-1690, 1873-1875, 1932-1934, 1959-1960, 1987, 2017, 2033-2036, 2081-2082, 2090, 2096, 2148-2149, 2255, 2259-2260, 2312, 2325-2326, 2328-2339, 2394-2395, 2408-2409, 2411, 2425-2426, 2471-2472, 2474, 2484-2485, 2496-2497, 2509, 2572-2573, 2663-2667, 2757-2763, 2803-2804, 2818, 2869, 2903-2907, 2929-2932, 3033-3034, 3317-3321, 3394-3395, 3427-3431, 3451-3455, 3484-3519, 3560-3561, 3574-3575, 3584, 3611-3612, 3646-3647, 3671-3672, 3712-3720, 3772-3773, 3826-3827, 3829-3830, 3843-3844, 3848-3867, 3965-3966, 4088-4094 |
| factory/chain/idle.py                                  |      209 |       39 |     81% |91-92, 148-150, 158, 169, 172-173, 184-185, 248-250, 271-272, 283-297, 317-318, 340, 365, 368, 392-404, 422 |
| factory/chain/issue\_intake.py                         |       46 |        5 |     89% |55, 89-90, 92-93 |
| factory/chain/orchestrator.py                          |      588 |      100 |     83% |329, 344-348, 493, 708-709, 886-909, 944-945, 961-962, 1009, 1017-1018, 1030-1031, 1040, 1048-1049, 1094-1096, 1101, 1211-1221, 1341-1344, 1357, 1364-1384, 1404-1405, 1427-1429, 1436-1438, 1446-1448, 1466-1471, 1486-1487, 1509-1513, 1523-1524, 1559-1566, 1586-1587, 1618, 1703-1713, 1758-1759, 1889-1890, 1922-1926, 1941-1944, 1947-1949, 1969-1970 |
| factory/chain/pm\_sync.py                              |      249 |       40 |     84% |161-162, 164-165, 167-169, 187, 196, 425, 457, 498-502, 506, 545, 561-573, 589-599, 615, 622, 632-634, 657-658, 669, 681-683, 757-758, 760, 770-771, 781 |
| factory/chain/review\_events.py                        |       11 |        0 |    100% |           |
| factory/chain/rollback.py                              |      106 |        3 |     97% |102-103, 105 |
| factory/chain/scheduled\_tasks.py                      |      278 |       31 |     89% |231, 240-243, 344, 374-375, 407, 607, 612, 621-623, 690-691, 714, 718, 721, 726-727, 779, 789, 792-793, 850-856 |
| factory/chain/security.py                              |        7 |        7 |      0% |     11-46 |
| factory/chain/slop\_detector.py                        |      257 |       38 |     85% |109-112, 135, 137, 156, 160, 164, 179, 185, 195, 202, 236-237, 258-259, 289, 296, 300, 331-346, 361, 368, 370, 433, 491, 499-500, 503 |
| factory/chain/state\_machine.py                        |      113 |        0 |    100% |           |
| factory/chain/step\_events.py                          |       59 |        9 |     85% |111-112, 127-128, 157, 160-161, 168-169 |
| factory/chain/ux\_auditor.py                           |        7 |        7 |      0% |     14-49 |
| factory/chain/worktree.py                              |      112 |       25 |     78% |134-135, 140, 169, 172-174, 190-191, 232, 235-241, 262, 279-280, 303, 307, 316-317, 325, 327-328 |
| factory/cli.py                                         |     1333 |      722 |     46% |45-46, 61, 88-140, 160-171, 185-201, 209-215, 224-245, 262-295, 304-339, 348-360, 387-390, 414-420, 442-490, 494-495, 500, 524-559, 566-589, 606, 615-617, 626-633, 658-680, 698-743, 751-766, 790, 862, 875-876, 885, 891, 921-922, 924, 939-940, 942-943, 945, 970-978, 980, 998-999, 1002-1004, 1006, 1024-1025, 1027, 1039-1078, 1107-1129, 1163-1164, 1262-1263, 1319-1323, 1363-1403, 1409-1414, 1438-1455, 1475-1550, 1554-1589, 1608-1618, 1640-1651, 1772-1804, 1816-1853, 1886, 1890-1898, 2016-2072, 2085-2112, 2130-2133, 2150, 2153, 2220-2275, 2315, 2349-2350, 2380-2386, 2442-2444, 2459-2482, 2505, 2508-2512, 2533-2540, 2544-2546, 2555-2556, 2562-2563, 2567, 2570-2571, 2576-2578, 2584-2585, 2709-2723, 2763-2778, 2824-2843, 2898-2923, 2942-2958, 2972-2992, 3014-3032, 3045-3071, 3111-3169, 3198-3199 |
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
| factory/git\_state.py                                  |       19 |        0 |    100% |           |
| factory/manager/\_\_init\_\_.py                        |        0 |        0 |    100% |           |
| factory/manager/apply.py                               |      460 |      174 |     62% |177-179, 193-195, 246-249, 288, 337, 342, 360, 363, 405-444, 456, 467-497, 506-563, 586, 599, 608, 658-665, 733-734, 755-761, 815-819, 829-832, 847-848, 853-855, 879-883, 913, 916-920, 924-928, 946-948, 958-960, 1001-1003, 1008, 1012-1017, 1092-1094, 1117, 1127, 1132-1133 |
| factory/manager/circuit\_breaker.py                    |      187 |       36 |     81% |126-127, 149, 154-157, 178-180, 195-197, 256-259, 267-272, 348-351, 372-376, 430-433, 437, 440, 467-468, 562-567 |
| factory/manager/detectors/\_\_init\_\_.py              |       15 |        0 |    100% |           |
| factory/manager/detectors/cost\_spike.py               |       50 |        4 |     92% |29, 32-33, 112 |
| factory/manager/detectors/placeholder\_prompts.py      |       32 |        1 |     97% |        66 |
| factory/manager/detectors/retry\_storm.py              |       44 |        3 |     93% | 67, 70-71 |
| factory/manager/detectors/review\_churn.py             |       52 |        5 |     90% |110, 113-114, 120, 123 |
| factory/manager/detectors/runs\_failed\_since.py       |       25 |        3 |     88% | 49, 52-53 |
| factory/manager/detectors/stalled\_stories.py          |      165 |       25 |     85% |56, 59-60, 62, 82, 85-86, 88, 92-93, 113, 116-117, 119, 123-124, 144, 147-148, 151-152, 172-173, 176, 279 |
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
| factory/observability/queries.py                       |      321 |       62 |     81% |150-151, 153, 203-206, 259-264, 271, 323-324, 338, 366-367, 461, 463-487, 535-538, 544, 592-595, 641-650, 653-659 |
| factory/observability/schema.py                        |       48 |        0 |    100% |           |
| factory/personas/\_\_init\_\_.py                       |        0 |        0 |    100% |           |
| factory/providers/\_\_init\_\_.py                      |        0 |        0 |    100% |           |
| factory/providers/azure\_foundry.py                    |       34 |        0 |    100% |           |
| factory/providers/github.py                            |       23 |        5 |     78% |     76-81 |
| factory/runner.py                                      |      707 |      108 |     85% |132-133, 152, 159-160, 343-344, 370-371, 435, 447, 458, 500, 534-535, 537-542, 561-562, 565-571, 632-633, 672-673, 689, 692, 694, 699-700, 708-723, 731, 734-740, 749-759, 764, 767, 806, 821, 858-859, 864-876, 958, 960-964, 1245, 1247, 1291-1314, 1360-1365, 1386, 1713, 1740-1741, 1749-1753, 1822, 1836-1837, 1840-1841 |
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
| **TOTAL**                                              | **14886** | **3095** | **79%** |           |


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