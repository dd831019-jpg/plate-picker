# 選牌標牌

全台監理所站可選車牌號碼與標牌公告（汽車 8 種、機車 6 種）。

- 網頁：`index.html`（由 `src/index.html` 壓縮而成），讀取 `data/car.json`（汽車）與 `data/moto.json`（機車，切換時才載入）
- 資料：`scripts/update_plates.py` 從 car16 抓取 14 種車種，和前一次比對標出新增號碼與當天被選走的號碼；資料檔用精簡格式（v2，號碼、監理站、車種分欄存放）
- 網址會記住目前的篩選條件（例如 `?t=電動自小客&p=o:桃園監理站&e=88`），可以直接分享
- 可加到手機主畫面（`manifest.webmanifest`、`sw.js`），沒網路時顯示上次看過的資料
- 網頁開著或從背景回來時，每 10 分鐘自動檢查一次新資料（資料沒變不會重新下載）
- 車牌依車種顯示實際號牌顏色；車牌號碼字型為 Barlow Condensed（`fonts/`，SIL Open Font License），中文使用裝置內建字型
- 每日更新：`.github/workflows/update.yml`，每 15 分鐘檢查一次（car16 沒更新就跳過），有變動才提交；也可以在 Actions 頁面按 Run workflow 手動更新
- 上線：Settings → Pages → Deploy from a branch → `main` / `(root)`
