// アプリが起動する前に、保存されたテーマを <html> へ復元する。
// これが無いと、ライトで一瞬描かれてからダークに切り替わる「ちらつき」が出る。
//
// ★ インラインの <script> にしないこと。
//   core/security_headers.py の CSP が script-src 'self' なので、
//   インラインスクリプトはブラウザに実行を拒否される。
//   同一オリジンの .js ファイルとして置けば通る。
//
// 「システムに従う」を選んでいる場合は何もしない。
// tokens.css の color-scheme: light dark が OS の設定を拾うので、
// JavaScript 抜きで正しい色から描き始められる。
(function () {
  try {
    var mode = localStorage.getItem('theme-mode');
    if (mode === 'light' || mode === 'dark') {
      document.documentElement.setAttribute('data-theme', mode);
    }
    // 配色も同じ理由で先に戻す。緑で一瞬描かれてから青や赤へ切り替わるのを防ぐ。
    // 既定の緑は属性なし。tokens.css の :root がそのまま効く。
    var palette = localStorage.getItem('theme-palette');
    if (palette === 'blue' || palette === 'red' || palette === 'yellow') {
      document.documentElement.setAttribute('data-palette', palette);
    }
  } catch (e) {
    // プライベートモードなどで localStorage が使えないことがある。
    // テーマは OS の設定に従うだけなので、握りつぶしてよい。
  }
})();
