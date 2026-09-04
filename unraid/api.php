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

$body = null;
if ($method === 'POST') {
    $body = file_get_contents('php://input', false, null, 0, 262144);
    if ($body === false) {
        $body = '';
    }
}
$socket = '/var/run/libvirt-balloon-keeper-api.sock';
$errno = 0;
$errstr = '';
$connection = @stream_socket_client('unix://' . $socket, $errno, $errstr, 5, STREAM_CLIENT_CONNECT);
if ($connection === false) {
    error_log('libvirt-balloon-keeper bridge response: route=' . $route . ' status=503 upstream=unavailable');
    http_response_code(503);
    header('Content-Type: application/json');
    echo '{"error":"keeper API unavailable"}';
    exit;
}
$request_headers = array(
    $method . ' ' . $target . ' HTTP/1.1',
    'Host: localhost',
    'Connection: close',
    'Accept: application/json, text/plain',
);
if ($body !== null) {
    $request_headers[] = 'Content-Type: text/plain; charset=utf-8';
    $request_headers[] = 'Content-Length: ' . strlen($body);
}
if (in_array($route, array('save', 'save-configuration'), true)) {
    $request_headers[] = 'X-Confirm: apply';
}
$request = implode("\r\n", $request_headers) . "\r\n\r\n" . ($body === null ? '' : $body);
$written = 0;
while ($written < strlen($request)) {
    $count = @fwrite($connection, substr($request, $written));
    if ($count === false || $count === 0) {
        fclose($connection);
        error_log('libvirt-balloon-keeper bridge response: route=' . $route . ' status=503 upstream=write-failed');
        http_response_code(503);
        header('Content-Type: application/json');
        echo '{"error":"keeper API unavailable"}';
        exit;
    }
    $written += $count;
}
stream_set_timeout($connection, 5);
$raw_response = stream_get_contents($connection);
fclose($connection);
$raw_response = $raw_response === false ? '' : $raw_response;
$separator = strpos($raw_response, "\r\n\r\n");
if ($separator === false || !preg_match('/^HTTP\/[^ ]+ ([0-9]{3})\b/', $raw_response, $match)) {
    error_log('libvirt-balloon-keeper bridge response: route=' . $route . ' status=502 upstream=malformed');
    http_response_code(502);
    header('Content-Type: application/json');
    echo '{"error":"keeper API returned an invalid response"}';
    exit;
}
$status = (int) $match[1];
$response = substr($raw_response, $separator + 4);
http_response_code($status);
error_log('libvirt-balloon-keeper bridge response: route=' . $route . ' status=' . $status);
header('Content-Type: ' . ($route === 'config' ? 'text/plain; charset=utf-8' : 'application/json'));
header('Cache-Control: no-store, no-cache, must-revalidate');
echo $response;
?>
