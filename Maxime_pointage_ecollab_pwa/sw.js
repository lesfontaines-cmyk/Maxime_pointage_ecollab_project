// =============================================
// Pointage CM — Service Worker
// =============================================
// Developpeur : bumper APP_VERSION a chaque deploiement.
// Ce seul changement declenche le cycle complet de mise a jour.
// =============================================

var APP_VERSION = '3.1.1';
var CACHE_NAME  = 'pointage-cm-v' + APP_VERSION;

var PRECACHE_FILES = [
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './favicon.ico',
  './favicon.png'
];

// ----- INSTALL -----
// Pre-cache les fichiers essentiels, puis activation immediate (skipWaiting).
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(PRECACHE_FILES);
    })
  );
  self.skipWaiting();
});

// ----- ACTIVATE -----
// Supprime TOUS les anciens caches, puis prend le controle des clients.
self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(cacheNames) {
      return Promise.all(
        cacheNames
          .filter(function(name) { return name !== CACHE_NAME; })
          .map(function(name) { return caches.delete(name); })
      );
    }).then(function() {
      return self.clients.claim();
    })
  );
});

// ----- FETCH -----
self.addEventListener('fetch', function(event) {
  var request = event.request;

  // Ignorer les requetes non-GET
  if (request.method !== 'GET') return;

  // Navigations HTML → stale-while-revalidate
  if (request.mode === 'navigate') {
    event.respondWith(
      caches.open(CACHE_NAME).then(function(cache) {
        return cache.match(request).then(function(cachedResponse) {
          // Toujours fetch en arriere-plan pour mettre a jour le cache
          var fetchPromise = fetch(request).then(function(networkResponse) {
            if (networkResponse && networkResponse.status === 200) {
              cache.put(request, networkResponse.clone());
            }
            return networkResponse;
          }).catch(function() {
            return null;
          });

          // Retourne le cache immediatement si disponible,
          // sinon attend le reseau
          return cachedResponse || fetchPromise;
        });
      })
    );
    return;
  }

  // Autres requetes → cache-first, fallback reseau
  event.respondWith(
    caches.match(request).then(function(cachedResponse) {
      if (cachedResponse) return cachedResponse;
      return fetch(request).then(function(networkResponse) {
        if (networkResponse && networkResponse.status === 200
            && request.url.startsWith(self.location.origin)) {
          var responseClone = networkResponse.clone();
          caches.open(CACHE_NAME).then(function(cache) {
            cache.put(request, responseClone);
          });
        }
        return networkResponse;
      });
    }).catch(function() {
      if (request.mode === 'navigate') {
        return caches.match('./index.html');
      }
    })
  );
});

// ----- MESSAGE -----
// Permet a la page de demander la version du SW
self.addEventListener('message', function(event) {
  if (event.data && event.data.type === 'GET_VERSION') {
    event.ports[0].postMessage({ version: APP_VERSION });
  }
});
