/* Code128B -> SVG. Нужен для листа возвратов: штрихкод сканируется на месте выдачи. */
const CODE128_PATTERNS = [
  '212222','222122','222221','121223','121322','131222','122213','122312','132212','221213',
  '221312','231212','112232','122132','122231','113222','123122','123221','223211','221132',
  '221231','213212','223112','312131','311222','321122','321221','312212','322112','322211',
  '212123','212321','232121','111323','131123','131321','112313','132113','132311','211313',
  '231113','231311','112133','112331','132131','113123','113321','133121','313121','211331',
  '231131','213113','213311','213131','311123','311321','331121','312113','312311','332111',
  '314111','221411','431111','111224','111422','121124','121421','141122','141221','112214',
  '112412','122114','122411','142112','142211','241211','221114','413111','241112','134111',
  '111242','121142','121241','114212','124112','124211','411212','421112','421211','212141',
  '214121','412121','111143','111341','131141','114113','114311','411113','411311','113141',
  '114131','311141','411131','211412','211214','211232','2331112',
];

function code128Widths(data) {
  const values = [104];
  for (const char of String(data)) {
    const code = char.charCodeAt(0);
    if (code < 32 || code > 126) continue;
    values.push(code - 32);
  }
  let checksum = values[0];
  for (let i = 1; i < values.length; i++) checksum += i * values[i];
  values.push(checksum % 103, 106);
  const widths = [];
  for (const value of values) {
    for (const digit of CODE128_PATTERNS[value]) widths.push(Number(digit));
  }
  return widths;
}

function code128Svg(data, { height = 34, module = 1.1 } = {}) {
  const widths = code128Widths(data);
  const total = widths.reduce((sum, width) => sum + width, 0);
  let x = 0;
  let dark = true;
  let rects = '';
  for (const width of widths) {
    if (dark) rects += `<rect x="${(x * module).toFixed(2)}" y="0" width="${(width * module).toFixed(2)}" height="${height}" fill="#000"/>`;
    x += width;
    dark = !dark;
  }
  const svgWidth = (total * module).toFixed(2);
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${svgWidth}" height="${height}" viewBox="0 0 ${svgWidth} ${height}">${rects}</svg>`;
}

function renderBarcodes(selector = '[data-barcode]') {
  document.querySelectorAll(selector).forEach((element) => {
    const value = element.dataset.barcode;
    if (!value) return;
    try {
      element.innerHTML = code128Svg(value, {
        height: Number(element.dataset.barcodeHeight || 30),
        module: Number(element.dataset.barcodeModule || 1),
      });
    } catch (e) { element.textContent = value; }
  });
}
