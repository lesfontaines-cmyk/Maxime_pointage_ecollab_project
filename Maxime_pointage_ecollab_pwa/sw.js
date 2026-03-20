// =============================================
// Pointage CM — Service Worker
// =============================================
// Developpeur : bumper APP_VERSION a chaque deploiement.
// Ce seul changement declenche le cycle complet de mise a jour.
// =============================================

var APP_VERSION = '3.8.0';
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

// ----- NOTIFICATION CLICK -----
// Ouvre l'app quand l'utilisateur clique sur une notification de cloture
self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
      for (var i = 0; i < clientList.length; i++) {
        if (clientList[i].url.indexOf('index.html') !== -1 || clientList[i].url.endsWith('/')) {
          return clientList[i].focus();
        }
      }
      return clients.openWindow('./');
    })
  );
});

// ----- BACKGROUND SYNC : CLOTURE -----
// Le navigateur déclenche cet événement même si l'app est fermée.
// Si le fetch échoue, on throw pour que le navigateur réessaie plus tard.
self.addEventListener('sync', function(event) {
  if (event.tag === 'cloture-sync') {
    event.waitUntil(_executerCloture());
  }
});

function _openClotureDB() {
  return new Promise(function(resolve, reject) {
    var req = indexedDB.open('cloture-db', 1);
    req.onupgradeneeded = function(e) {
      e.target.result.createObjectStore('pending', { keyPath: 'id' });
    };
    req.onsuccess = function(e) { resolve(e.target.result); };
    req.onerror = function(e) { reject(e.target.error); };
  });
}

function _getPendingCloture() {
  return _openClotureDB().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction('pending', 'readonly');
      var req = tx.objectStore('pending').get('current');
      req.onsuccess = function() { resolve(req.result || null); };
      req.onerror = function() { reject(req.error); };
    });
  });
}

function _clearPendingCloture() {
  return _openClotureDB().then(function(db) {
    return new Promise(function(resolve, reject) {
      var tx = db.transaction('pending', 'readwrite');
      var req = tx.objectStore('pending').delete('current');
      req.onsuccess = function() { resolve(); };
      req.onerror = function() { reject(req.error); };
    });
  });
}

function _executerCloture() {
  return _getPendingCloture().then(function(job) {
    if (!job) return;

    return fetch(job.server + '/cloture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email:     job.email,
        password:  job.password,
        url:       job.url,
        plages:    job.plages,
        date:      job.date,
        variables: job.variables || {}
      })
    })
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      // Succès ou erreur métier — on arrête les retries
      return _clearPendingCloture().then(function() {
        if (data.success) {
          var plagesLabel = job.plages.length
            ? job.plages.map(function(p) { return p.debut + ' - ' + p.fin; }).join(' | ')
            : 'Journée vide';
          return self.registration.showNotification('Clôture réussie', {
            body: job.dateLabel + ' : ' + plagesLabel,
            icon: './icon-192.png',
            badge: './icon-192.png',
            tag: 'cloture-result',
            renotify: true
          });
        } else {
          return self.registration.showNotification('Échec de la clôture', {
            body: job.dateLabel + ' : ' + (data.error || 'Erreur inconnue'),
            icon: './icon-192.png',
            badge: './icon-192.png',
            tag: 'cloture-result',
            renotify: true,
            requireInteraction: true
          });
        }
      });
    })
    .catch(function(e) {
      // Fetch échoué (réseau / serveur down) — throw pour que le navigateur réessaie
      console.log('[SW] Cloture sync échoué, le navigateur réessaiera:', e.message);
      throw e;
    });
  });
}

// ----- MESSAGE -----
self.addEventListener('message', function(event) {
  if (event.data && event.data.type === 'GET_VERSION') {
    event.ports[0].postMessage({ version: APP_VERSION });
  }
  // La page peut aussi demander de stocker une clôture en attente
  if (event.data && event.data.type === 'STORE_CLOTURE') {
    event.waitUntil(
      _openClotureDB().then(function(db) {
        return new Promise(function(resolve, reject) {
          var tx = db.transaction('pending', 'readwrite');
          var store = tx.objectStore('pending');
          var job = event.data.payload;
          job.id = 'current';
          store.put(job);
          tx.oncomplete = function() { resolve(); };
          tx.onerror = function() { reject(tx.error); };
        });
      })
    );
  }
});
