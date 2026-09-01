<?php
/**
 * Same-origin WebGUI bridge to the loopback-only keeper API.
 * Only the named routes below are forwarded; this is not an open proxy.
 */
$routes = array(
    'status' => array('GET', '/api/status'),
    'inventory' => array('GET', '/api/inventory'),
    'config' => array('GET', '/api/config'),
    'validate' => array('POST', '/api/validate'),
    'save' => array('POST', '/api/config'),
    'validate-configuration' => array('POST', '/api/validate-configuration'),
    'save-configuration' => array('POST', '/api/configuration'),
);
$route = isset($_GET['route']) ? $_GET['route'] : '';
$method = $_SERVER['REQUEST_METHOD'];
error_log('libvirt-balloon-keeper bridge request: ' . $method . ' route=' . $route);

if ($route === 'audit' && $method === 'GET') {
    $vm = isset($_GET['vm']) ? $_GET['vm'] : '';
    $limit = isset($_GET['limit']) ? $_GET['limit'] : '20';
    if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/', $vm) || !preg_match('/^(?:[1-9]|[1-9][0-9]|100)$/', $limit)) {
        http_response_code(400);
        header('Content-Type: application/json');
        echo '{"error":"invalid audit query"}';
        exit;
    }
    $target = '/api/audit?vm=' . rawurlencode($vm) . '&limit=' . rawurlencode($limit);
} elseif (isset($routes[$route]) && $routes[$route][0] === $method) {
    if (in_array($route, array('save', 'save-configuration'), true) && (!isset($_SERVER['HTTP_X_CONFIRM']) || $_SERVER['HTTP_X_CONFIRM'] !== 'apply')) {
        http_response_code(428);
        header('Content-Type: application/json');
        echo '{"error":"confirmation required"}';
        exit;
    }
    $target = $routes[$route][1];
} else {
    http_response_code(404);
    header('Content-Type: application/json');
    echo '{"error":"unknown route"}';
    exit;
}

$headers = array('Accept: application/json, text/plain');
$body = null;
if ($method === 'POST') {
    $body = file_get_contents('php://input', false, null, 0, 262144);
    if ($body === false) {
        $body = '';
    }
    $headers[] = 'Content-Type: text/plain; charset=utf-8';
    if (in_array($route, array('save', 'save-configuration'), true)) {
        $headers[] = 'X-Confirm: apply';
    }
}
$options = array(
    'http' => array(
        'method' => $method,
        'header' => implode("\r\n", $headers),
        'ignore_errors' => true,
        'timeout' => 5,
    ),
);
if ($body !== null) {
    $options['http']['content'] = $body;
}
$response = @file_get_contents('http://127.0.0.1:8765' . $target, false, stream_context_create($options));
if ($response === false) {
    error_log('libvirt-balloon-keeper bridge response: route=' . $route . ' status=503 upstream=unavailable');
    http_response_code(503);
    header('Content-Type: application/json');
    echo '{"error":"keeper API unavailable"}';
    exit;
}
$status = 502;
if (isset($http_response_header[0]) && preg_match('/\s([0-9]{3})\s/', $http_response_header[0], $match)) {
    $status = (int) $match[1];
}
http_response_code($status);
error_log('libvirt-balloon-keeper bridge response: route=' . $route . ' status=' . $status);
header('Content-Type: ' . ($route === 'config' ? 'text/plain; charset=utf-8' : 'application/json'));
header('Cache-Control: no-store, no-cache, must-revalidate');
echo $response;
?>
