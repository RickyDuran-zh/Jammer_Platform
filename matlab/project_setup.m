function project_setup()
projectRoot = fileparts(mfilename('fullpath'));
addpath(genpath(fullfile(projectRoot, 'backend')));
addpath(genpath(fullfile(projectRoot, 'interface')));
addpath(genpath(fullfile(projectRoot, 'radio')));
addpath(genpath(fullfile(projectRoot, 'sensing')));
addpath(genpath(fullfile(projectRoot, 'waveform')));
disp("MATLAB 项目路径加载完成。");
end
