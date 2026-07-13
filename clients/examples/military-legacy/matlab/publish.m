function publish(suffix, body, varargin)
%PUBLISH  Publish a value to an EFDI pod from MATLAB, via the local REST bridge.
%
%   This is NOT a Zenoh client. It POSTs an HTTP body to the REST bridge
%   (clients/examples/bridges/rest-http/), which runs next to the pod on 127.0.0.1
%   and does the Zenoh mTLS publish. We use only MATLAB's built-in WEBWRITE — no
%   toolboxes, no compiled MEX, no internet.
%
%       POST http://127.0.0.1:8080/pub/<suffix>   body -> published to <namespace>/<suffix>
%
%   Usage:
%       publish('sensors/temp', '{"temp_c":21.5}')
%       publish('sensors/temp', '21.5')
%       publish('sensors/temp', '21.5', 'Count', 10, 'IntervalSec', 0.2)
%
%   The bridge base URL defaults to http://127.0.0.1:8080; override with the
%   BRIDGE_URL environment variable or the 'BridgeUrl' option.
%
%   A bare suffix like 'sensors/temp' is scoped under your namespace by the bridge
%   (release/<you>/sensors/temp). The pod, the bridge, and MATLAB all run on the
%   same box — this call never leaves localhost.

    p = inputParser;
    addRequired(p, 'suffix', @(x) ischar(x) || isstring(x));
    addRequired(p, 'body',   @(x) ischar(x) || isstring(x));
    addParameter(p, 'Count', 1, @(x) isnumeric(x) && x >= 1);
    addParameter(p, 'IntervalSec', 0, @isnumeric);
    addParameter(p, 'BridgeUrl', bridgeBase(), @(x) ischar(x) || isstring(x));
    parse(p, suffix, body, varargin{:});

    base    = char(p.Results.BridgeUrl);
    suffix  = char(p.Results.suffix);
    body    = char(p.Results.body);
    count   = p.Results.Count;
    interval = p.Results.IntervalSec;

    url = [base '/pub/' suffix];

    % Send the body as a raw octet-stream so the bridge publishes the exact bytes.
    % MediaType + CharacterEncoding make WEBWRITE put 'body' in the request body verbatim.
    opts = weboptions('RequestMethod', 'post', ...
                       'MediaType', 'application/octet-stream', ...
                       'CharacterEncoding', 'UTF-8', ...
                       'Timeout', 30);

    for i = 1:count
        % webwrite returns the bridge's response body ({"published":...,"bytes":N}).
        resp = webwrite(url, body, opts);
        if isstruct(resp)
            fprintf('published %s/%s (%d bytes)\n', '<namespace>', suffix, resp.bytes);
        else
            fprintf('published -> %s : %s\n', url, char(resp));
        end
        if i < count && interval > 0
            pause(interval);
        end
    end
end

function base = bridgeBase()
    base = getenv('BRIDGE_URL');
    if isempty(base)
        base = 'http://127.0.0.1:8080';
    end
end
