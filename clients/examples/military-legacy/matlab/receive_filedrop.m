function receive_filedrop(inboxDir, varargin)
%RECEIVE_FILEDROP  Receive fabric data in MATLAB by polling the file-drop bridge's INBOX.
%
%   The most universal, no-network path. The file-drop bridge
%   (clients/examples/bridges/file-drop/) subscribes to the fabric and writes each
%   sample into INBOX_DIR as a file named by its key (slashes -> '__') plus a
%   millisecond timestamp, e.g.:
%
%       release__partner-b__sensors__temp.1717000000123.bin
%
%   Files appear ATOMICALLY (the bridge writes a '.'-prefixed temp then renames), so
%   a file you can see is complete. This function polls the directory, reads each new
%   .bin file's bytes, and hands them to you. NO network call, NO Zenoh client —
%   if MATLAB can read a directory, it can receive from the fabric.
%
%   To PUBLISH the file-drop way, just write a file into the bridge's OUTBOX_DIR; its
%   path under the outbox becomes the key. From MATLAB:
%       fid = fopen(fullfile(outbox,'sensors','temp'),'w'); fwrite(fid, body); fclose(fid);
%   (write to a temp name then MOVEFILE it in, so the bridge never sees a partial file.)
%
%   Usage:
%       receive_filedrop('./inbox')                       % poll forever (Ctrl-C to stop)
%       receive_filedrop('./inbox', 'PollSec', 0.5)
%       receive_filedrop('./inbox', 'MaxSamples', 5)      % stop after 5
%       receive_filedrop('./inbox', 'Callback', @(key,bytes) disp(key))
%
%   INBOX_DIR here must match the bridge's INBOX_DIR. Everything is on the same box.

    p = inputParser;
    addRequired(p, 'inboxDir', @(x) ischar(x) || isstring(x));
    addParameter(p, 'PollSec', 1.0, @isnumeric);
    addParameter(p, 'MaxSamples', Inf, @isnumeric);
    addParameter(p, 'Callback', [], @(x) isempty(x) || isa(x, 'function_handle'));
    addParameter(p, 'DeleteAfterRead', false, @islogical);
    parse(p, inboxDir, varargin{:});

    inboxDir = char(p.Results.inboxDir);
    pollSec  = p.Results.PollSec;
    maxN     = p.Results.MaxSamples;
    cb       = p.Results.Callback;
    delAfter = p.Results.DeleteAfterRead;

    if ~isfolder(inboxDir)
        error('receive_filedrop:noInbox', ...
              'inbox dir "%s" does not exist (is the file-drop bridge running with this INBOX_DIR?)', ...
              inboxDir);
    end

    seen = containers.Map('KeyType', 'char', 'ValueType', 'logical');
    n = 0;
    fprintf('polling %s for inbound samples (Ctrl-C to stop)\n', inboxDir);

    while n < maxN
        % Only complete files match *.bin; the bridge's in-progress temp files start with '.'.
        listing = dir(fullfile(inboxDir, '*.bin'));
        for i = 1:numel(listing)
            fname = listing(i).name;
            if seen.isKey(fname)
                continue;   % already processed this one
            end
            seen(fname) = true;

            full = fullfile(inboxDir, fname);
            key  = keyFromFilename(fname);
            bytes = readAllBytes(full);

            if isempty(cb)
                txt = tryUtf8(bytes);
                if isempty(txt)
                    fprintf('%s  (%d bytes binary)\n', key, numel(bytes));
                else
                    fprintf('%s  %s\n', key, txt);
                end
            else
                cb(key, bytes);
            end

            if delAfter
                delete(full);
            end

            n = n + 1;
            if n >= maxN
                break;
            end
        end
        if n < maxN
            pause(pollSec);
        end
    end
end

function key = keyFromFilename(fname)
%KEYFROMFILENAME  Reverse the bridge's naming: 'a__b__c.<ms>.bin' -> 'a/b/c'.
    stem = fname;
    % strip the trailing ".<ms>.bin"
    stem = regexprep(stem, '\.\d+\.bin$', '');
    key  = strrep(stem, '__', '/');
end

function bytes = readAllBytes(path)
    fid = fopen(path, 'r');
    if fid < 0
        bytes = uint8([]);
        return;
    end
    cleanup = onCleanup(@() fclose(fid));
    bytes = fread(fid, Inf, '*uint8')';
end

function txt = tryUtf8(bytes)
%TRYUTF8  Decode bytes as UTF-8 text; return '' if they aren't valid text.
    if isempty(bytes)
        txt = '';
        return;
    end
    try
        txt = native2unicode(bytes, 'UTF-8');
        % Reject obvious binary: any NUL or C0 control char other than tab(9)/LF(10)/CR(13).
        c = double(txt);
        if any(c == 0) || any(c < 32 & c ~= 9 & c ~= 10 & c ~= 13)
            txt = '';
        end
    catch
        txt = '';
    end
end
