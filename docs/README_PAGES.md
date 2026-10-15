# Publicar el dashboard con GitHub Pages

La carpeta `docs/` contiene un sitio estático listo para GitHub Pages:

| Archivo | Para qué sirve |
|---|---|
| `index.html` | Dashboard interactivo (un solo archivo, Chart.js desde CDN) |
| `data/dashboard.json` | Agregados que lee el dashboard (sin filas crudas) |
| `data/dashboard.js` | Mismos datos, para abrir `index.html` con doble clic (`file://`) |
| `screenshot.png` | Captura usada en el README |
| `.nojekyll` | Evita que GitHub procese el sitio con Jekyll |

## Activar GitHub Pages (una sola vez)

1. Sube el repositorio a GitHub (rama `main`) incluyendo la carpeta `docs/` completa.
2. En el repositorio entra a **Settings → Pages**.
3. En **Build and deployment → Source** elige **Deploy from a branch**.
4. En **Branch** selecciona **`main`** y la carpeta **`/docs`**, y da clic en **Save**.
5. Espera 1-2 minutos. El sitio queda en:
   `https://fpadillagarcia100-oss.github.io/analisis-transacciones/`

El repositorio debe ser público (o tener un plan que permita Pages en repos privados).

## Actualizar los datos del dashboard

Cada vez que cambien los datos o el SQL, regenera el JSON y haz commit:

```powershell
python python/exportar_dashboard.py
git add docs/data
git commit -m "Actualiza datos del dashboard"
git push
```

GitHub Pages se vuelve a publicar solo después del `push`.

## Probarlo en tu computadora

```powershell
python -m http.server 8000 -d docs
# abre http://localhost:8000
```

También puedes abrir `docs/index.html` con doble clic: en ese caso los datos se cargan desde `data/dashboard.js`.
Se necesita internet para descargar Chart.js; sin él, cada gráfica conserva su tabla ("View as table").
