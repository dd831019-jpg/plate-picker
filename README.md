# 選牌標牌

全台監理所站可選車牌號碼（電動自小客、電動租賃車、租賃小客貨車）與標牌公告。

- 網頁：`index.html`，讀取同目錄的 `plates.json`
- 資料：`scripts/update_plates.py` 從 car16 抓取，並和前一天比對標出新增號碼
- 每日更新：`.github/workflows/update.yml`，每小時執行一次，有變動才提交；也可以在 Actions 頁面按 Run workflow 手動更新
- 上線：Settings → Pages → Deploy from a branch → `main` / `(root)`
