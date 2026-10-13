// Test unitaire de la logique autoSizeTextarea extraite de app/static/app.js
const fs = require('fs');
const src = fs.readFileSync('app/static/app.js', 'utf8');
const m = src.match(/function autoSizeTextarea\(el\)\{[^\n]+\}/);
if (!m) { console.error('FAIL: fonction autoSizeTextarea introuvable'); process.exit(1); }
eval(m[0]);

function makeTextarea({ intrinsic, border, content }) {
  // Mock d'un textarea en box-sizing:border-box :
  // - hauteur intrinsèque (attribut rows) quand style.height vaut 'auto' ou '';
  // - clientHeight = hauteur padding-box ; offsetHeight = clientHeight + bordures ;
  // - scrollHeight = max(contenu, clientHeight) comme dans les navigateurs.
  const el = {
    tagName: 'TEXTAREA',
    dataset: {},
    style: {},
    _intrinsic: intrinsic,
    _contentHeight: content, // hauteur padding-box nécessaire au contenu
    get usedHeight() {
      return (this.style.height === 'auto' || !this.style.height) ? this._intrinsic : parseFloat(this.style.height);
    },
    get offsetHeight() { return this.usedHeight + border; },
    get clientHeight() { return this.usedHeight; },
    get scrollHeight() { return Math.max(this._contentHeight, this.usedHeight); },
  };
  return el;
}

let ok = true;
function check(name, cond) { console.log((cond ? 'OK  ' : 'FAIL') + ' ' + name); if (!cond) ok = false; }

// 1. Contenu plus haut que la hauteur initiale (rows=5 ~ 118px) : grandit avec le contenu.
let el = makeTextarea({ intrinsic: 118, border: 2, content: 260 });
autoSizeTextarea(el);
check('grandit avec le contenu (260+2px)', el.style.height === '262px');

// 2. Contenu plus court que la hauteur initiale : ne descend pas sous la hauteur rows.
el = makeTextarea({ intrinsic: 118, border: 2, content: 40 });
autoSizeTextarea(el);
check('garde le plancher rows (118+2px)', el.style.height === '120px');

// 3. Appel suivant (contenu réduit) : le plancher mémorisé persiste.
el._contentHeight = 30;
autoSizeTextarea(el);
check('plancher mémorisé stable (118+2px)', el.style.height === '120px');

// 4. Appel suivant (contenu agrandi) : suit à nouveau le contenu.
el._contentHeight = 400;
autoSizeTextarea(el);
check('suit le contenu après agrandissement (400+2px)', el.style.height === '402px');

// 5. Élément masqué (panel display:none → clientHeight 0) : no-op, plancher non capturé.
el = makeTextarea({ intrinsic: 118, border: 2, content: 260 });
Object.defineProperty(el, 'usedHeight', { get() { return 0; } }); // non mesurable
autoSizeTextarea(el);
check('no-op si non mesurable (clientHeight=0)', el.style.height === undefined && el.dataset.autosizeFloor === undefined);

// 6. Élément non-textarea : no-op sans erreur.
const input = { tagName: 'INPUT', dataset: {}, style: {} };
autoSizeTextarea(input);
check('no-op sur INPUT', input.style.height === undefined);

// 7. Null : no-op sans erreur.
autoSizeTextarea(null);
check('no-op sur null', true);

process.exit(ok ? 0 : 1);
