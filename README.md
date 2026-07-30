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
| factory/chain/\_\_init\_\_.py                          |        2 |        0 |    100% |           |
| factory/chain/acceptance.py                            |      166 |       35 |     79% |88-89, 95-98, 148, 189, 191, 200-228, 236-237, 320, 350, 357-358, 371-372, 381-382, 384-385, 388-389 |
| factory/chain/auto\_merge.py                           |      653 |      107 |     84% |294-295, 342, 344, 346, 350-351, 510-511, 513, 550, 567, 663-664, 752, 853-854, 1144-1145, 1155, 1173-1174, 1176, 1179-1180, 1188, 1219-1220, 1222, 1247, 1249, 1258-1259, 1270-1271, 1294, 1336, 1339-1340, 1342, 1350, 1379-1380, 1384-1385, 1405-1406, 1409, 1481, 1495-1496, 1539-1540, 1548-1549, 1558-1559, 1568-1569, 1641-1642, 1712, 1866, 1885-1886, 1961-1962, 2016, 2041-2093, 2116, 2247-2248, 2271-2272, 2291-2295, 2316-2324, 2342-2346, 2397-2398, 2434-2437, 2461-2462, 2484-2485, 2496-2497, 2536-2537 |
| factory/chain/branch.py                                |       56 |        4 |     93% |   163-166 |
| factory/chain/bug\_hunter.py                           |        8 |        8 |      0% |     13-62 |
| factory/chain/ci\_health.py                            |      197 |       28 |     86% |113-114, 131, 134, 178, 181, 201, 210, 217, 251, 264, 326, 330, 339, 343-344, 421-422, 430-431, 449-450, 468-469, 494-495, 505-506 |
| factory/chain/context\_refresh.py                      |      179 |       40 |     78% |123-130, 211, 222-248, 277-290, 360-369, 411, 442, 477, 524-526, 541-543, 545-548, 553-556, 564-565, 567, 571-572 |
| factory/chain/dual\_draft.py                           |      150 |       15 |     90% |64-65, 197, 228, 230, 234, 383, 401-402, 444-445, 476-477, 479-480 |
| factory/chain/ears.py                                  |       71 |        0 |    100% |           |
| factory/chain/event\_log.py                            |       55 |        3 |     95% |122, 132-133 |
| factory/chain/factory\_improver.py                     |      274 |       92 |     66% |142, 159-160, 162, 167-168, 184-223, 256-257, 264-265, 332, 375-397, 411-412, 437-438, 515-516, 537, 563, 631-669, 686-688, 702-714, 741-784 |
| factory/chain/factory\_improver\_apply.py              |      355 |       39 |     89% |120, 204, 240, 253, 321, 386-387, 458-459, 469, 498-499, 546, 553-554, 577-578, 600, 637, 693-694, 722, 782, 815-824, 921-922, 925, 935-944, 958, 1031-1035 |
| factory/chain/factory\_status.py                       |      144 |        8 |     94% |85-86, 118-119, 121, 264, 286-288 |
| factory/chain/gates/\_\_init\_\_.py                    |        3 |        0 |    100% |           |
| factory/chain/gates/acceptance\_verified.py            |       40 |        2 |     95% |   161-162 |
| factory/chain/gates/canonical\_paths\_only.py          |       10 |        0 |    100% |           |
| factory/chain/gates/docs\_current.py                   |       23 |        3 |     87% | 19, 24-25 |
| factory/chain/gates/evaluator.py                       |       55 |        5 |     91% |128, 158-163 |
| factory/chain/gates/smoke\_green.py                    |       15 |        0 |    100% |           |
| factory/chain/gates/tests\_green.py                    |       38 |       10 |     74% |47-63, 122, 143 |
| factory/chain/gates/tests\_meaningful.py               |      100 |       17 |     83% |88, 103, 157, 160-161, 196, 216-217, 221, 240-249, 253 |
| factory/chain/handlers.py                              |     1374 |      246 |     82% |151-152, 186-187, 360-372, 450-464, 471-472, 498-499, 511-522, 544-545, 552-564, 583-584, 656-657, 661-662, 799-802, 804-807, 811-812, 854, 1007, 1141, 1163-1165, 1179-1180, 1259, 1339-1345, 1381-1382, 1413-1414, 1418-1420, 1584, 1606, 1613, 1621-1622, 1624-1627, 1687-1688, 1698-1705, 1707-1715, 1893-1895, 1948-1950, 1975-1976, 2003, 2033, 2049-2052, 2096-2097, 2105, 2111, 2163-2164, 2268, 2272-2273, 2321, 2334-2335, 2337-2348, 2403-2404, 2417-2418, 2420, 2434-2435, 2480-2481, 2483, 2493-2494, 2505-2506, 2518, 2543-2563, 2625-2626, 2716-2720, 2804-2810, 2850-2851, 2862, 2913, 2943-2947, 2965-2968, 3065-3066, 3349-3353, 3447-3448, 3494-3498, 3516-3518, 3547-3580, 3630-3631, 3664-3665, 3670, 3703-3704, 3741-3742, 3859-3860, 3866, 3886, 3889-3890, 3895, 3951-3952, 3992-4000, 4052-4053, 4121-4122, 4124-4125, 4138-4139, 4143-4162, 4260-4261, 4387-4395 |
| factory/chain/idle.py                                  |      209 |       39 |     81% |91-92, 148-150, 158, 169, 172-173, 184-185, 248-250, 271-272, 283-297, 317-318, 340, 365, 368, 392-404, 422 |
| factory/chain/issue\_intake.py                         |       46 |        5 |     89% |55, 89-90, 92-93 |
| factory/chain/orchestrator.py                          |      852 |      176 |     79% |342, 357-361, 524, 619, 631, 832-833, 1008-1037, 1048-1073, 1149, 1156, 1200-1228, 1263-1264, 1280-1281, 1299, 1315, 1328-1329, 1376, 1384-1385, 1397-1398, 1407, 1413-1414, 1459-1461, 1466, 1549-1550, 1592, 1641-1651, 1742, 1764, 1815-1816, 1822-1823, 1858-1859, 1864-1865, 1923-1924, 1933-1934, 1985-1988, 2001, 2008-2029, 2049-2050, 2072-2074, 2086-2088, 2100-2102, 2127-2134, 2139-2141, 2149-2151, 2167-2172, 2187-2188, 2210-2214, 2224-2225, 2260-2267, 2287-2288, 2442-2452, 2497-2498, 2628-2629, 2661-2665, 2680-2683, 2703-2713, 2716-2718, 2739-2740 |
| factory/chain/pm\_sync.py                              |      249 |       40 |     84% |161-162, 164-165, 167-169, 187, 196, 425, 457, 498-502, 506, 545, 561-573, 589-599, 615, 622, 632-634, 657-658, 669, 681-683, 757-758, 760, 770-771, 781 |
| factory/chain/review\_events.py                        |       11 |        0 |    100% |           |
| factory/chain/rollback.py                              |      106 |        3 |     97% |102-103, 105 |
| factory/chain/scheduled\_tasks.py                      |      281 |       34 |     88% |231, 240-243, 344, 374-375, 407, 608-610, 625, 630, 639-641, 708-709, 732, 736, 739, 744-745, 797, 807, 810-811, 875-881 |
| factory/chain/security.py                              |        7 |        7 |      0% |     11-46 |
| factory/chain/slop\_detector.py                        |      301 |       39 |     87% |113, 137-140, 166, 168, 185, 189, 193, 208, 214, 224, 233, 269-270, 288-289, 319, 326, 330, 361-376, 391, 398, 400, 551, 609, 617-618, 621 |
| factory/chain/state\_machine.py                        |      117 |        0 |    100% |           |
| factory/chain/step\_events.py                          |       59 |        9 |     85% |111-112, 127-128, 157, 160-161, 168-169 |
| factory/chain/ux\_auditor.py                           |        7 |        7 |      0% |     14-49 |
| factory/chain/worktree.py                              |      112 |       25 |     78% |134-135, 140, 169, 172-174, 190-191, 232, 235-241, 262, 279-280, 303, 307, 316-317, 325, 327-328 |
| factory/cli.py                                         |     1572 |      896 |     43% |46-47, 62, 90-142, 162-173, 187-203, 211-217, 226-247, 264-297, 338-373, 390-429, 438-450, 477-480, 504-510, 532-580, 584-585, 590, 614-649, 656-679, 696, 705-707, 716-723, 748-770, 794, 796, 798-803, 805-817, 819, 827, 856-871, 895, 969, 980, 993-994, 1003, 1009, 1039-1040, 1042, 1057-1058, 1060-1061, 1063, 1088-1096, 1098, 1116-1117, 1120-1122, 1124, 1142-1143, 1145, 1157-1197, 1226-1248, 1265-1274, 1305-1329, 1346-1364, 1375-1390, 1417-1418, 1516-1517, 1573-1577, 1617-1657, 1663-1668, 1692-1709, 1729-1805, 1809-1844, 1863-1873, 1895-1906, 1939, 1964, 1996, 2062-2094, 2106-2143, 2176, 2180-2188, 2306-2362, 2375-2402, 2420-2423, 2440, 2443, 2510-2565, 2605, 2639-2640, 2670-2676, 2732-2734, 2749-2772, 2795, 2798-2802, 2823-2830, 2834-2836, 2845-2846, 2852-2853, 2857, 2860-2861, 2866-2868, 2874-2875, 2999-3013, 3053-3068, 3114-3133, 3188-3213, 3232-3248, 3262-3282, 3304-3322, 3335-3361, 3401-3459, 3488-3489, 3562-3602, 3631-3699, 3730-3817 |
| factory/context/\_\_init\_\_.py                        |        0 |        0 |    100% |           |
| factory/context/canonical\_paths.py                    |       36 |        2 |     94% |     90-91 |
| factory/context/enforcer.py                            |       49 |        0 |    100% |           |
| factory/context/loader.py                              |       58 |        1 |     98% |       133 |
| factory/context/navigator.py                           |       39 |        2 |     95% |     65-66 |
| factory/context/updater.py                             |       29 |        1 |     97% |        77 |
| factory/deploy/\_\_init\_\_.py                         |        4 |        0 |    100% |           |
| factory/deploy/models.py                               |       28 |        0 |    100% |           |
| factory/deploy/orchestrator.py                         |      284 |       34 |     88% |257, 259-262, 265-267, 316, 402-405, 431-441, 486-490, 532-536, 555-556, 561, 578, 589, 596, 704-705 |
| factory/deploy/runner.py                               |       56 |        0 |    100% |           |
| factory/directions/\_\_init\_\_.py                     |        0 |        0 |    100% |           |
| factory/directions/backfill.py                         |      100 |        7 |     93% |40, 44, 54-55, 57, 73, 100 |
| factory/directions/creator.py                          |      150 |       69 |     54% |126, 156, 163, 197, 214-222, 232-343 |
| factory/directions/gc.py                               |       73 |       10 |     86% |59, 63, 76, 79-80, 82, 136-137, 165-166 |
| factory/directions/ingester.py                         |       77 |        2 |     97% |   55, 131 |
| factory/directions/parser.py                           |      242 |       21 |     91% |58, 148, 158-159, 173, 201-203, 258, 264-265, 269, 291, 297, 307, 333, 346, 358, 383, 396-398 |
| factory/directions/schema.py                           |       49 |        0 |    100% |           |
| factory/directions/tracker\_issue.py                   |      226 |       23 |     90% |70-72, 110, 116, 120, 195-199, 325, 329, 346, 353-354, 420-422, 446-447, 450, 487 |
| factory/directions/watcher.py                          |      102 |       24 |     76% |128, 159, 161-162, 182-186, 194-204, 209-214 |
| factory/events/\_\_init\_\_.py                         |        1 |        0 |    100% |           |
| factory/events/rotation.py                             |       78 |       16 |     79% |62-63, 67-69, 96-98, 113, 116-117, 123-124, 144, 163-164 |
| factory/git\_state.py                                  |       41 |        2 |     95% |    47, 60 |
| factory/manager/\_\_init\_\_.py                        |        0 |        0 |    100% |           |
| factory/manager/apply.py                               |      472 |      168 |     64% |192-194, 208-211, 262-265, 304, 372, 377, 381, 399, 402, 444-483, 495, 506-536, 545-602, 625, 638, 647, 697-704, 772-773, 796-803, 857-861, 930-931, 935-936, 941-943, 1001, 1004-1008, 1012-1016, 1034-1037, 1049-1051, 1092-1094, 1099, 1103-1108, 1183-1186, 1209, 1219, 1224-1225 |
| factory/manager/circuit\_breaker.py                    |      187 |       36 |     81% |126-127, 149, 154-157, 178-180, 195-197, 256-259, 267-272, 348-351, 372-376, 430-433, 437, 440, 467-468, 562-567 |
| factory/manager/detectors/\_\_init\_\_.py              |       17 |        0 |    100% |           |
| factory/manager/detectors/conformance\_breach.py       |       24 |        8 |     67% |60-61, 65-66, 69-72, 79-80 |
| factory/manager/detectors/cost\_spike.py               |       50 |        4 |     92% |29, 32-33, 112 |
| factory/manager/detectors/fms\_yield.py                |       63 |        6 |     90% |84, 87-88, 90, 94-95 |
| factory/manager/detectors/placeholder\_prompts.py      |       32 |        1 |     97% |        66 |
| factory/manager/detectors/retry\_storm.py              |       44 |        3 |     93% | 67, 70-71 |
| factory/manager/detectors/review\_churn.py             |       52 |        5 |     90% |110, 113-114, 120, 123 |
| factory/manager/detectors/runs\_failed\_since.py       |       25 |        3 |     88% | 49, 52-53 |
| factory/manager/detectors/stalled\_stories.py          |      165 |       25 |     85% |58, 61-62, 64, 84, 87-88, 90, 94-95, 115, 118-119, 121, 125-126, 146, 149-150, 153-154, 174-175, 178, 281 |
| factory/manager/detectors/state\_distribution\_skew.py |       41 |        5 |     88% |77, 80-81, 83, 89 |
| factory/manager/detectors/tick\_duration\_outliers.py  |       70 |        8 |     89% |23, 25-26, 87, 90-91, 95, 117 |
| factory/manager/detectors/worktree\_orphans.py         |       38 |        4 |     89% |64-65, 86-87 |
| factory/manager/diagnostician.py                       |      466 |      101 |     78% |235-236, 248-249, 348-349, 356, 363, 402-403, 405, 548-550, 559, 570-571, 576, 581-583, 586, 594, 653-657, 738-739, 753, 794-797, 826-831, 880-881, 907-908, 953-955, 974-975, 1037-1042, 1044-1049, 1156-1157, 1169-1170, 1191-1192, 1224-1312 |
| factory/manager/escalation.py                          |      177 |       21 |     88% |126-128, 138-139, 152-153, 167, 289-290, 292, 296, 346, 409-410, 455-456, 493-494, 531-532 |
| factory/manager/halt.py                                |      126 |       24 |     81% |101-102, 180-185, 247, 293-296, 314-315, 317, 322-324, 332-337 |
| factory/manager/poison\_escalation.py                  |      109 |       16 |     85% |93-94, 99, 102-103, 127, 173, 181, 183, 315-324, 345-346, 360-361 |
| factory/manager/recovery.py                            |      467 |       63 |     87% |182, 185-186, 188, 228-232, 245, 248, 250, 295-296, 353, 364-365, 411, 416-417, 471, 484-485, 499, 553-554, 565-566, 614, 631-632, 670, 673-674, 677, 811, 821-822, 829, 838-839, 851, 960-961, 980, 983-984, 987-988, 1004, 1025, 1027, 1057-1058, 1128-1129, 1346-1365 |
| factory/manager/self\_context.py                       |      141 |       29 |     79% |43-45, 55-57, 170-171, 189, 200, 203, 206-207, 212-213, 257-258, 292-298, 323, 338-339, 388, 393-400 |
| factory/manager/signals.py                             |       95 |        5 |     95% |146-147, 167-168, 330 |
| factory/manager/staging.py                             |      152 |       11 |     93% |206, 307-308, 394, 437, 452, 470, 562-583 |
| factory/manager/summarizer.py                          |      414 |      104 |     75% |42-44, 55-57, 118, 123, 147, 150-151, 155-156, 180, 183-184, 204, 207-208, 212-213, 215, 219, 221-222, 293, 305, 308-309, 311, 332-333, 573-576, 578, 584-606, 647-649, 670-671, 674-675, 776-778, 783-785, 883-963 |
| factory/manager/watcher.py                             |      407 |      124 |     70% |41-43, 54-56, 130, 133-134, 138-139, 172, 189, 204, 207-208, 210-211, 222, 226, 228-229, 293-304, 437-440, 442, 450-472, 540-541, 548-549, 559-560, 565-566, 573-574, 581-582, 587-588, 597-598, 610-611, 676-677, 680-683, 799-826, 835-841, 858-865, 886-889, 908-921, 942, 947, 975-976, 978, 999, 1041-1054, 1065-1098 |
| factory/model\_router.py                               |      122 |        9 |     93% |53, 55, 67, 89, 100, 189-192 |
| factory/observability/\_\_init\_\_.py                  |        0 |        0 |    100% |           |
| factory/observability/audit\_chain.py                  |      216 |       26 |     88% |170-187, 248, 327, 331, 358-359, 373, 382-383, 455-460, 549-550 |
| factory/observability/conformance.py                   |      148 |       11 |     93% |120, 181, 209, 215, 225, 306, 351-356 |
| factory/observability/estimator.py                     |      185 |       31 |     83% |170-185, 219, 242-244, 331, 333, 337, 384, 417, 450, 470, 489, 491, 493, 498 |
| factory/observability/heartbeat.py                     |       60 |        7 |     88% |69, 74-77, 129-130 |
| factory/observability/queries.py                       |      321 |       62 |     81% |150-151, 153, 205-208, 261-266, 273, 325-326, 340, 368-369, 463, 465-489, 537-540, 546, 594-597, 643-652, 655-661 |
| factory/observability/schema.py                        |       58 |        2 |     97% |   118-119 |
| factory/observability/state\_trace.py                  |       99 |       22 |     78% |111-112, 125-127, 139-140, 189, 195-196, 200-201, 218-219, 248, 251-252, 254, 256, 258, 260-261 |
| factory/personas/\_\_init\_\_.py                       |        0 |        0 |    100% |           |
| factory/personas/loader.py                             |       86 |        5 |     94% |124-125, 151-152, 187 |
| factory/personas/validator.py                          |      107 |       10 |     91% |89, 115, 154, 176, 211, 221, 278-279, 298-299 |
| factory/power.py                                       |      140 |        3 |     98% |83, 94, 168 |
| factory/providers/\_\_init\_\_.py                      |        0 |        0 |    100% |           |
| factory/providers/azure\_foundry.py                    |       34 |        0 |    100% |           |
| factory/providers/github.py                            |       23 |        2 |     91% |     79-81 |
| factory/runner.py                                      |      721 |      105 |     85% |132-133, 152, 159-160, 372-373, 399-400, 464, 476, 502, 540, 574-575, 577-582, 601-602, 605-611, 672-673, 712-713, 729, 732, 734, 739-740, 748-763, 771, 774-780, 789-799, 804, 807, 846, 861, 898-899, 904-916, 998, 1000-1004, 1285, 1287, 1402-1407, 1428, 1779, 1807-1808, 1816-1820, 1893, 1916-1917, 1920-1921 |
| factory/runtime\_state.py                              |       51 |        0 |    100% |           |
| factory/scheduler/\_\_init\_\_.py                      |        0 |        0 |    100% |           |
| factory/scheduler/cron.py                              |      132 |       11 |     92% |117, 122, 124, 171-175, 211, 353-354 |
| factory/settings/\_\_init\_\_.py                       |        0 |        0 |    100% |           |
| factory/settings/audit.py                              |      124 |        5 |     96% |129-130, 132, 161-162 |
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
| **TOTAL**                                              | **17107** | **3501** | **80%** |           |


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