# Piljek 2023: T-Motor G30/G32 bench tables (open data, CC0 1.0)

Measured propeller and motor tables for the propeller validation runner
(`tools/props/validate.py`) and its CI test (`tools/props/tests/test_public.py`).

**Source.** Piljek, Petar, 2023, "UAV Multirotor Propulsion - Experimental Data",
Harvard Dataverse, V1, https://doi.org/10.7910/DVN/J5L2P4. Licence: CC0 1.0 (public
domain dedication), https://creativecommons.org/publicdomain/zero/1.0/. Companion paper:
Kotarski, Piljek, Kasać, IEEE Access 11 (2023). No positions or flight data: bench
measurements only.

**What it is.** An RCbenchmark 1780 stand sweeping the ESC's PWM from 1000 to 2000 µs in
100 µs steps, up and down twice, 5 s a step, with T-Motor components on a 12-cell pack at
3.8, 4.0 and 4.2 V a cell:

| Setups | Motor | ESC | Propellers |
| --- | --- | --- | --- |
| 1, 2 | AG1005 90Kv | Flame 60A | G30×10.5, G32×11 |
| 3, 4 | P80 100Kv | Alpha 80A | G30×10.5, G32×11 |
| 5, 6 | U13 130Kv | Alpha 120A | G30×10.5, G32×11 |
| 7, 8 | U13 130Kv | Flame 180A | G30×10.5, G32×11 |

The dataset's description gives setups 5 and 7 as "G32×10.5". Their thrust coefficient
on a 30-inch disc (0.079, against 0.081 for the G30 on the other motors and 0.10 for every
G32) shows a G30×10.5, as T-Motor's G series has no 32×10.5; they are read as G30×10.5.

**How the tables were made** (`tools/props/piljek.py`, from the 24 source CSVs). Each step
averages its four readings (thrust, torque as its magnitude, optical RPM, voltage,
current); the load cell's and the torque sensor's zero, from the readings at 1000 µs
interpolated in time, is taken off; steps under 300 rpm are left out. The ambient
temperature is not logged for every setup, so every table is read in standard air (the same
for all, so it cancels in the ratios the predictions are made of).

**Source files** (SHA-256 of each CSV as downloaded on 2026-10-31):

```
83bd01e5b60570b19c826a324e09921030ce4b92a5794f83fcdaeb1bb629525c  setup1_measurement_3.8V.csv
a0fd76369b0c1de9852d06b0621e375083d4cb8bf67e6cefa96ab0edd2e10e76  setup1_measurement_4.0V.csv
b80d4bf3686fc2900767e74991e4d636dc9e6985d92d9c36cdadbdc4c9c5f92c  setup1_measurement_4.2V.csv
fc857f3be10840cfa405f86e9c61f4abde462d6b639673b76670b0f7d4dbca6b  setup2_measurement_3.8V.csv
a5050050ae23593adac50ae92fafd41a570ebe0e3b38b602377990a8972c55e4  setup2_measurement_4.0V.csv
ce038e3f02e6213ea49ccf0cf8fdf2c0ddc090a39d6eca1a0606f500ddfe7111  setup2_measurement_4.2V.csv
31ae880d06637e8780b64a6b34e6fbc0828d5618f2a9a65bf4c1c6c5fab515bd  setup3_measurement_3.8V.csv
9d90497f5e13d5e0e449b04fc0850a167027caebcf0db9eb0f2f25448dd53feb  setup3_measurement_4.0V.csv
a59b8bb83226aa6f0108bd6fe5c6c40399e687b9f1d6fde349a2276694ec3ef8  setup3_measurement_4.2V.csv
4ab1146450b8ceedf5ef23dc61b18ff904d19ec1fb488afee0f5b7ace71b334b  setup4_measurement_3.8V.csv
d08f5332fffa4b51dff0d7a278e42da0d30dd64f29bb1c0029987350e46ab47f  setup4_measurement_4.0V.csv
c1c78b2db9d126249929b30a5cd9f9c1e997df1ab5030fcfeaa0a49cc5e369ef  setup4_measurement_4.2V.csv
c18a2e1d051ce5561aacd61e93c79d396b7b5d227d33a7605cdd2bf5adae0fe5  setup5_measurement_3.8V.csv
7a76b874b8812ba503084557001f8b5ce74a5ce3fdc356822ac515e795b7249f  setup5_measurement_4.0V.csv
df61d3db48c3f27a646a302e99907c9a958a3787deedddc1015e9fbce7f43ced  setup5_measurement_4.2V.csv
b90952c2472b737fb6675a1b941ec8ea79b15313d3281f8a7a64539017b3657c  setup6_measurement_3.8V.csv
07f96e9045c3b7cd7c4f1f3c8be24bbd1834140bb5bf266b69cf755abaf4fdad  setup6_measurement_4.0V.csv
115ab7d3407b72c0dacfc47d925555add7ef993e4fb03fa642efddb114c46c4c  setup6_measurement_4.2V.csv
70bb0ca16654b1ffa4a287e24dc8c14b82793fc32ca1850286d9b3e5ec41ad3f  setup7_measurement_3.8V.csv
6aec254d9c322ae5c2978a6c5c92d87019fea6a967e69f1821f8c6b61d363575  setup7_measurement_4.0V.csv
ca76999d8ffa74e57bfba5b96c4592877b4fb203e0e74bfe360aa2d4c08673e0  setup7_measurement_4.2V.csv
c46844e06333ad867fdf9ebe1759465a032497c96fe9cc82b4eafbfd86152586  setup8_measurement_3.8V.csv
c4b93738413cceb12b5afcd933024bfca031353f90c22c1130d60f5a25fad16d  setup8_measurement_4.0V.csv
4be215699e7393acd4bd75c95b74a69e0a75c86c300d87c7b67094e8f64b4f92  setup8_measurement_4.2V.csv
```
