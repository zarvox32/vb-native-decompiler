This utility allows for modding the sounds of troopanum 2.0.

making a mod:
You do not need the original sound files for this to work, only know the filenames you want to replace. This utility provides them.
The sound names folder lists the internal filenames along with a description for each. The list is separated into files based on category for organization.
Choose any folder to use, and place wav files with the filenames you want to replace. For example "ship5.wav" for the liquidator movement sound.
The utility will recursively search sub directories in this folder, so you can structure the files any way you like. The end result does not care about folder structure.

applying the mod:
Place the tr20_soundmod.exe file in the same directory as the game. This utility will replace "tr20.bsc", so that file must be present or it exits.
When running the utility, it will ask for a folder path. If you want to use a shortcut, it automatically looks for a folder called "soundmod" in the game's directory.
It reports how many files are being changed, and if the process is successful.
When finished it asks where you want the modded file to be placed:
1 overwrite the game's current bsc file, does not save a copy in your mod's folder.
2 overwrite the game's current bsc file, and save a copy in your mod's folder.
3 save a copy in your mods folder, does not overwrite the game's current bsc file.
Press enter to close the terminal after answering.