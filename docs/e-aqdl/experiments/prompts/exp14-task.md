/aq-dlc:start inventory に、在庫の少ない商品を CSV 形式の文字列で返す関数 export_low_stock_csv(items, threshold=10) を追加してください。
- items は fetch_items が返す形のリスト
- 在庫数が threshold 未満の商品だけを、在庫数の少ない順（同じなら sku の順）に出す
- 1行目は見出し `sku,name,stock`。改行は `\n`。該当がなければ見出しの行だけ
- name にカンマや引用符が含まれていても、CSV として正しく読めること
- stock が整数でない商品があれば ValueError、threshold が負の数でも ValueError
