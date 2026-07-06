// =============================================================================
// WorkPulse — CloudFront Function: Clean URLs
// =============================================================================
// Viewer Request: reescreve URLs amigáveis para os caminhos reais no S3.
//
// Mapeamentos ativos:
//   /              → /index.html          (root)
//   /dashboard     → /dashboard/workforce.html
//   /adm           → /adm/admin.html
//   /docs          → /docs/developers.html
//
// Fallback genérico:
//   Qualquer path sem extensão (/foo/bar) tenta /foo/bar.html
//
// Importante: arquivos com extensão (ex: .js, .css, .jpeg) passam
//             sem modificação — nunca afeta assets estáticos.
// =============================================================================

var ROUTES = {
    '/':          '/index.html',
    '/dashboard': '/dashboard/workforce.html',
    '/adm':       '/adm/admin.html',
    '/docs':      '/docs/developers.html',
};

function handler(event) {
    var uri = event.request.uri;

    // Remove trailing slash para match limpo (exceto root "/")
    var clean = (uri.length > 1 && uri.endsWith('/'))
        ? uri.slice(0, -1)
        : uri;

    // 1. Match exato na tabela de rotas
    if (ROUTES[clean] !== undefined) {
        event.request.uri = ROUTES[clean];
        return event.request;
    }

    // 2. Fallback genérico: paths sem extensão → tenta .html
    //    Ex: /dashboard/alguma-coisa → /dashboard/alguma-coisa.html
    var lastSegment = clean.split('/').pop();
    if (lastSegment && lastSegment.indexOf('.') === -1) {
        event.request.uri = clean + '.html';
        return event.request;
    }

    // 3. Asset com extensão → passa sem modificação
    return event.request;
}
