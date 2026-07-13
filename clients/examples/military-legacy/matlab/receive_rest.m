function samples = receive_rest(keyexpr, varargin)
%RECEIVE_REST  Receive N samples from an EFDI pod via the local REST bridge.
%
%   This is NOT a Zenoh client. It does an HTTP GET against the REST bridge
%   (clients/examples/bridges/rest-http/), which holds the Zenoh mTLS identity and
%   subscribes on our behalf. Uses only MATLAB's built-in WEBREAD — no toolboxes.
%
%       GET http://127.0.0.1:8080/sub/<keyexpr>?count=N&timeout=S
%       -> blocks until N samples arrive (or timeout), returns a JSON array
%
%   Usage:
%       s = receive_rest('sensors/temp')               % wait for 1 sample
%       s = receive_rest('sensors/temp', 'Count', 5)   % wait for 5
%       s = receive_rest('release/<partner>/**', 'Count', 3, 'TimeoutSec', 60)
%
%   Returns a struct array; each element has fields:
%       .key   - the full fabric key,  e.g. 'release/acme/sensors/temp'
%       .ts    - bridge receive time (epoch seconds)
%       .text  - the payload as text  (or .b64 if the bytes were not valid UTF-8)
%
%   MATLAB's WEBREAD auto-decodes the JSON the bridge returns into this struct array.
%   Everything is localhost; this call never leaves the box.

    p = inputParser;
    addRequired(p, 'keyexpr', @(x) ischar(x) || isstring(x));
    addParameter(p, 'Count', 1, @(x) isnumeric(x) && x >= 1);
    addParameter(p, 'TimeoutSec', 30, @isnumeric);
    addParameter(p, 'BridgeUrl', bridgeBase(), @(x) ischar(x) || isstring(x));
    parse(p, keyexpr, varargin{:});

    base       = char(p.Results.BridgeUrl);
    keyexpr    = char(p.Results.keyexpr);
    count      = p.Results.Count;
    timeoutSec = p.Results.TimeoutSec;

    url = [base '/sub/' keyexpr];

    % The HTTP read timeout must exceed the bridge's blocking window, or WEBREAD aborts
    % before the samples arrive. Give it the bridge timeout + a margin.
    opts = weboptions('Timeout', timeoutSec + 10, 'ContentType', 'json');

    % Query string carries count + the bridge-side block timeout.
    samples = webread(url, 'count', count, 'timeout', timeoutSec, opts);

    % WEBREAD returns a struct array (or a single struct for one element). Normalize and report.
    if isempty(samples)
        fprintf('no samples within %d s for %s\n', timeoutSec, keyexpr);
        return;
    end
    if ~isstruct(samples)
        % A cell array can come back if the bridge ever returns mixed shapes; pass through.
        return;
    end
    for i = 1:numel(samples)
        s = samples(i);
        if isfield(s, 'text')
            fprintf('%s  %s\n', s.key, s.text);
        elseif isfield(s, 'b64')
            fprintf('%s  (binary, base64) %s\n', s.key, s.b64);
        else
            fprintf('%s  (unrecognized sample shape)\n', s.key);
        end
    end
end

function base = bridgeBase()
    base = getenv('BRIDGE_URL');
    if isempty(base)
        base = 'http://127.0.0.1:8080';
    end
end
